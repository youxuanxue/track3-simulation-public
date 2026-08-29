"""Trace-forensics for a gate rejection: localize the FIRST place a candidate trace / ledger
diverges from the reference, so you can see exactly which event your accelerated port got wrong.

Dev-side only — this does NOT enter the eval image and is never part of scoring. It uses RAPIDS
`cudf` when available (fast on large traces) and falls back to `pandas` automatically, so it runs
anywhere. cuDF stays out of the `network=none` submission image; this is a debugging aid on your own
machine.

Usage:
    python scripts/analyze_trace_cudf.py --candidate <dir-or-trace> --reference <dir-or-trace>

Each argument may be a unit directory (containing trace.parquet + optional message_trace.parquet) or
a direct path to a trace.parquet. Tier-A admissibility requires the fill sequence to match exactly
(order_id / msg_type / side / price / size) with timestamps within ``--tol-ns`` (default 1000).
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

# Columns whose exact, in-order equality the Tier-A fill-sequence gate requires.
_FILL_COLS = ["order_id", "msg_type", "side", "price", "size"]


def _backend() -> tuple[Any, str]:
    """Return (dataframe module, name). Prefer cudf; fall back to pandas."""
    try:
        import cudf  # type: ignore

        return cudf, "cudf"
    except Exception:
        import pandas as pd

        return pd, "pandas"


def _to_pandas(obj: Any) -> Any:
    """cudf DataFrame/Series -> pandas; pandas passes through."""
    return obj.to_pandas() if hasattr(obj, "to_pandas") else obj


def _resolve_trace(arg: str, name: str = "trace.parquet") -> Path:
    p = Path(arg)
    return p / name if p.is_dir() else p


def first_trace_divergence(
    cand: Any, ref: Any, *, tol_ns: int
) -> dict[str, Any] | None:
    """First row index where the candidate fill sequence differs from the reference, or None if the
    common prefix matches. Reports the differing columns and both rows. A length mismatch beyond a
    matching prefix is reported at the first surplus/missing index."""
    n = min(len(cand), len(ref))
    c = cand.iloc[:n].reset_index(drop=True)
    r = ref.iloc[:n].reset_index(drop=True)

    mask: Any = None
    for col in _FILL_COLS:
        if col not in c.columns or col not in r.columns:
            continue
        ne = c[col] != r[col]
        mask = ne if mask is None else (mask | ne)
    if "t_ns" in c.columns and "t_ns" in r.columns:
        t_ne = (c["t_ns"] - r["t_ns"]).abs() > tol_ns
        mask = t_ne if mask is None else (mask | t_ne)

    mask_pd = _to_pandas(mask)
    if mask_pd is not None and bool(mask_pd.any()):
        idx = int(mask_pd.values.argmax())
        crow = _to_pandas(c.iloc[idx : idx + 1]).to_dict("records")[0]
        rrow = _to_pandas(r.iloc[idx : idx + 1]).to_dict("records")[0]
        differing = [
            col
            for col in _FILL_COLS + ["t_ns"]
            if col in crow and crow[col] != rrow[col]
        ]
        return {
            "kind": "row_mismatch",
            "index": idx,
            "differing_columns": differing,
            "candidate": crow,
            "reference": rrow,
        }

    if len(cand) != len(ref):
        longer = "candidate" if len(cand) > len(ref) else "reference"
        extra = _to_pandas((cand if longer == "candidate" else ref).iloc[n : n + 1])
        return {
            "kind": "length_mismatch",
            "index": n,
            "candidate_rows": int(len(cand)),
            "reference_rows": int(len(ref)),
            "first_surplus_in": longer,
            "first_surplus_row": extra.to_dict("records")[0] if len(extra) else None,
        }
    return None


def check_message_ledger(cand_msg: Any, ref_msg: Any) -> dict[str, Any]:
    """Light message-ledger checks: latency identity on the candidate (t_recv - t_send ==
    latency_ns, where t_send is present) and the first seq/order_id divergence vs the reference."""
    out: dict[str, Any] = {}
    cm = _to_pandas(cand_msg)
    sent = cm[cm["t_send_ns"].notna()] if "t_send_ns" in cm.columns else cm.iloc[0:0]
    if len(sent):
        bad = sent[(sent["t_recv_ns"] - sent["t_send_ns"]) != sent["latency_ns"]]
        out["latency_identity_violations"] = int(len(bad))
        if len(bad):
            out["first_latency_violation"] = bad.iloc[0:1].to_dict("records")[0]
    if ref_msg is not None:
        rm = _to_pandas(ref_msg)
        k = min(len(cm), len(rm))
        for col in ("seq", "order_id", "msg_type", "latency_ns"):
            if col in cm.columns and col in rm.columns:
                ne = cm[col].iloc[:k].values != rm[col].iloc[:k].values
                if ne.any():
                    i = int(ne.argmax())
                    out.setdefault(
                        "first_ledger_divergence", {"index": i, "column": col}
                    )
                    break
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="analyze_trace_cudf")
    ap.add_argument("--candidate", required=True)
    ap.add_argument("--reference", required=True)
    ap.add_argument("--tol-ns", type=int, default=1000)
    args = ap.parse_args(argv)

    df, backend = _backend()
    print(f"[backend: {backend}]")

    cand = df.read_parquet(_resolve_trace(args.candidate))
    ref = df.read_parquet(_resolve_trace(args.reference))

    div = first_trace_divergence(cand, ref, tol_ns=args.tol_ns)
    if div is None:
        print("trace: fill sequence matches within tolerance ✓")
    elif div["kind"] == "row_mismatch":
        print(
            f"trace: FIRST DIVERGENCE at row {div['index']} "
            f"(columns: {', '.join(div['differing_columns'])})"
        )
        print(f"  candidate: {div['candidate']}")
        print(f"  reference: {div['reference']}")
    else:
        print(
            f"trace: LENGTH MISMATCH — candidate has {div['candidate_rows']} rows, "
            f"reference has {div['reference_rows']}; first surplus in {div['first_surplus_in']} "
            f"at row {div['index']}"
        )

    cand_msg_path = _resolve_trace(args.candidate, "message_trace.parquet")
    ref_msg_path = _resolve_trace(args.reference, "message_trace.parquet")
    if cand_msg_path.exists():
        led = check_message_ledger(
            df.read_parquet(cand_msg_path),
            df.read_parquet(ref_msg_path) if ref_msg_path.exists() else None,
        )
        print(f"ledger: {led}")

    return 0 if div is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
