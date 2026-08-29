"""BatchMarketSim (family GB) scoring: the isolation gate + aggregate anti-inflation.

A batch unit bundles N independent single-market sub-scenarios (``scenarios/<sub>.json``) together
with their ISOLATED reference traces (``checks/reference_data/<sub>/``). A submission runs the
batch verb ``simulate-batch`` and writes ``/output/<sub>/{trace,message_trace}.parquet`` +
``events.json`` per sub plus ``/output/batch_events.json``.

The isolation gate is: every sub's candidate output must reproduce its isolated reference — the
exact per-sub semantic pass + message-ledger checks the rest of the suite already uses. A batched
port that lets book / agent / RNG state leak between markets produces a divergent sub-trace and
fails. Shared by the public g3 gate (``scoring.py``) and the private offline oracle
(``final_scorer.py``) so the two agree exactly, like every other family.

## The ledger split is now by DECLARATION, not by file existence

Self-consistency is candidate-only and runs on EVERY sub. Reference-equivalence and family-7
protocol fidelity need a reference ledger, and a wide batch deliberately ships them for a sample of
its subs (the ledger is roughly three quarters of a sub's reference bytes, which is what makes
batches of hundreds of markets storable).

The sample used to be discovered by asking whether
``checks/reference_data/<sub>/message_trace.parquet``
happened to exist. That means **deleting a reference ledger silently disables the gate**, and the
failure mode points the wrong way: the check quietly stops running rather than complaining. Each
sub's ``batch.json`` entry already declares ``reference_message_sha256``, so the declaration is
what decides. A sub that declares one and does not have one is an organizer fault
(:class:`~qfbench2_track_simulation.semantics.ReferenceIncomplete`), not a skipped check.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import pandas as pd

from .semantics import (
    ReferenceIncomplete,
    check_message_reference,
    check_message_semantics,
    check_protocol_fidelity,
    semantic_regression_pass,
)


def batch_meta(unit_dir: str | Path) -> dict[str, Any] | None:
    """Return the ``[batch]`` card table if ``unit_dir`` is a batch unit, else ``None``."""
    cp = Path(unit_dir) / "card.toml"
    if not cp.exists():
        return None
    tbl = tomllib.loads(cp.read_text()).get("batch", {})
    return tbl if tbl.get("batch") else None


def load_subs(unit_dir: str | Path) -> list[dict[str, Any]]:
    """The sub-scenario entries from ``batch.json`` (each carries ``sub`` + reference shas)."""
    subs: list[dict[str, Any]] = json.loads(
        (Path(unit_dir) / "batch.json").read_text()
    )["subs"]
    return subs


def declared_reference_event_count(subs: list[dict[str, Any]]) -> int | None:
    """The organizer's deterministic total event count for a batch, from ``batch.json``.

    ``None`` when any sub omits ``n_events``, so a partially-declared batch does not silently
    produce a smaller expected total than the reference really has.
    """
    total = 0
    for entry in subs:
        value = entry.get("n_events")
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
        total += value
    return total


def score_subs(
    subs: list[dict[str, Any]],
    references_root: str | Path,
    output_dir: str | Path,
    family: int,
    tier: str | None,
    ceilings: dict[str, float],
    *,
    timestamp_tolerance_ns: int = 1_000,
    kendall_tau_floor: float = 0.999,
) -> tuple[bool, list[dict[str, Any]]]:
    """Isolation gate over an explicit sub list + references root — the shared core.

    ``references_root/<sub>/`` holds each isolated reference, ``output_dir/<sub>/`` the candidate's
    per-sub output. The public g3 gate passes
    ``references_root = unit_dir/checks/reference_data``
    (self-contained unit); the private oracle passes ``references_root = reference_dir/<label>``
    (sealed layout). One divergent sub fails the whole batch (a leak anywhere is inadmissible).

    Raises :class:`ReferenceIncomplete` when a sub's declared reference material is absent — that
    is ours, and the caller turns it into a whole-evaluation organizer fault rather than a
    participant verdict.
    """
    references_root = Path(references_root)
    output_dir = Path(output_dir)
    failures: list[dict[str, Any]] = []
    for entry in subs:
        sub = entry["sub"]
        cand_t = output_dir / sub / "trace.parquet"
        ref_t = references_root / sub / "trace.parquet"
        if not ref_t.exists():
            raise ReferenceIncomplete(
                f"batch sub {sub!r} has no reference trace at {ref_t.name}"
            )
        if not cand_t.exists():
            failures.append({"sub": sub, "reason": "missing candidate trace.parquet"})
            continue
        cand, ref = pd.read_parquet(cand_t), pd.read_parquet(ref_t)
        # Event-count identity: the isolated reference is deterministic, so a faithful reproduction
        # emits exactly as many events. Also checked against the organizer's DECLARED count where
        # batch.json carries one, so a reference that has drifted from its declaration is caught.
        declared = entry.get("n_events")
        if (
            isinstance(declared, int)
            and not isinstance(declared, bool)
            and declared != len(ref)
        ):
            raise ReferenceIncomplete(
                f"batch sub {sub!r} declares n_events={declared} but its reference trace has "
                f"{len(ref)} row(s)"
            )
        if len(cand) != len(ref):
            failures.append(
                {
                    "sub": sub,
                    "isolation": "event_count",
                    "cand_rows": len(cand),
                    "ref_rows": len(ref),
                }
            )
            continue
        ok, breaches = semantic_regression_pass(
            cand,
            ref,
            family=family,
            tier=tier,
            ceilings=ceilings,
            timestamp_tolerance_ns=timestamp_tolerance_ns,
            kendall_tau_floor=kendall_tau_floor,
        )
        if not ok:
            failures.append(
                {"sub": sub, "isolation": "semantic", "breaches": breaches[:2]}
            )
            continue
        # Self-consistency (latency identity, contiguous 0..N-1 seq permutation, causal
        # produced-before-consumed ordering, unique message ids, wakeup structure) is
        # CANDIDATE-ONLY, so it runs on EVERY sub. `message_trace.parquet` is a required output for
        # every batch sub-scenario (README.md "Submission format").
        cand_m = output_dir / sub / "message_trace.parquet"
        if not cand_m.exists():
            failures.append(
                {"sub": sub, "reason": "missing candidate message_trace.parquet"}
            )
            continue
        cand_msg = pd.read_parquet(cand_m)
        ok_s, br_s = check_message_semantics(cand_msg)
        if not ok_s:
            failures.append(
                {"sub": sub, "isolation": "message_semantics", "breaches": br_s[:2]}
            )
            continue

        # Reference-equivalence runs where the sub DECLARES a reference ledger. Declared-and-absent
        # is our fault; undeclared is a deliberate sampling decision recorded in batch.json.
        declares_ledger = bool(entry.get("reference_message_sha256"))
        ref_m = references_root / sub / "message_trace.parquet"
        if declares_ledger and not ref_m.exists():
            raise ReferenceIncomplete(
                f"batch sub {sub!r} declares reference_message_sha256 but ships no reference "
                "message ledger; the gate must not be disabled by a missing file"
            )
        if declares_ledger:
            ref_msg = pd.read_parquet(ref_m)
            ok_r, br_r = check_message_reference(cand_msg, ref_msg)
            if not ok_r:
                failures.append(
                    {"sub": sub, "isolation": "message_reference", "breaches": br_r[:2]}
                )
                continue
            if family == 7:
                ok_p, br_p = check_protocol_fidelity(cand_msg, ref_msg)
                if not ok_p:
                    failures.append(
                        {
                            "sub": sub,
                            "isolation": "protocol_fidelity",
                            "breaches": br_p[:2],
                        }
                    )
    return (not failures, failures)


def score_isolation(
    unit_dir: str | Path,
    output_dir: str | Path,
    family: int,
    tier: str | None,
    ceilings: dict[str, float],
    *,
    timestamp_tolerance_ns: int = 1_000,
    kendall_tau_floor: float = 0.999,
) -> tuple[bool, list[dict[str, Any]]]:
    """Isolation gate for a self-contained batch unit dir (public g3 path): the sub list comes from
    ``unit_dir/batch.json`` and each reference from ``unit_dir/checks/reference_data/<sub>/``."""
    unit_dir = Path(unit_dir)
    return score_subs(
        load_subs(unit_dir),
        unit_dir / "checks" / "reference_data",
        output_dir,
        family,
        tier,
        ceilings,
        timestamp_tolerance_ns=timestamp_tolerance_ns,
        kendall_tau_floor=kendall_tau_floor,
    )


def check_aggregate(
    output_dir: str | Path,
    declared_subs: list[dict[str, Any]],
    tol: float = 0.05,
) -> tuple[bool, dict[str, Any]]:
    """Aggregate anti-inflation on ``batch_events.json``. ``declared_subs`` is the unit's ``batch.json``
    sub list. Requires: ``per_scenario`` covers EXACTLY the declared subs (no extras, no duplicates);
    the ranked ``events_per_sec`` equals ``total_events / wall_clock_sec``; each ``per_scenario.n_events``
    equals that sub's real row count; and ``total_events`` equals their sum — so neither the aggregate
    throughput nor the event total can be fabricated (e.g. by padding ``per_scenario`` with a huge dummy
    sub, or listing a real sub twice, after running the real subs honestly).

    This remains a CROSS-CHECK on the submission's own arithmetic. It is not the ranked number: the
    ranked events/sec comes from the C2 run record's host-measured timing and Runner-measured row
    counts (see :mod:`qfbench2_track_simulation.telemetry`)."""
    output_dir = Path(output_dir)
    bep = output_dir / "batch_events.json"
    if not bep.exists():
        return False, {"reason": "missing batch_events.json"}
    be = json.loads(bep.read_text())
    # per_scenario must be EXACTLY the declared batch subs — the aggregate is only trustworthy over
    # the same subs the isolation gate (score_subs) actually validated.
    seen = [e.get("sub") for e in be.get("per_scenario", [])]
    declared = {e["sub"] for e in declared_subs}
    if len(seen) != len(set(seen)):
        return False, {
            "reason": "duplicate sub in batch_events per_scenario",
            "per_scenario_subs": seen,
        }
    if set(seen) != declared:
        return False, {
            "reason": "per_scenario subs must be exactly the declared batch subs (no extras/duplicates)",
            "reported": sorted(x for x in set(seen) if x is not None),
            "declared": sorted(declared),
        }
    # A missing key and a non-numeric value are different mistakes and used to produce the same
    # sentence. `wall_clock_sec` is required and was undocumented until 2026-08-27, so the most
    # likely reader of this message was someone who had never been told to write the field -- and
    # "non-numeric" sent them to look at the type of a value that was not there.
    required_numeric = ("total_events", "wall_clock_sec", "events_per_sec")
    absent = [k for k in required_numeric if k not in be]
    if absent:
        return False, {
            "reason": f"batch_events is missing required field(s): {', '.join(absent)}",
            "required": list(required_numeric),
        }
    try:
        total, wc, eps = (float(be[k]) for k in required_numeric)
    except (TypeError, ValueError) as exc:
        bad = [
            k
            for k in required_numeric
            if not isinstance(be[k], (int, float)) or isinstance(be[k], bool)
        ]
        return False, {
            "reason": f"non-numeric batch_events fields: {', '.join(bad) or exc}",
            "fields": {k: type(be[k]).__name__ for k in required_numeric},
        }
    if total <= 0 or wc <= 0 or eps < 0:
        return False, {
            "reason": "non-positive batch_events fields",
            "total_events": total,
            "wall_clock_sec": wc,
        }
    if abs(eps - total / wc) / (total / wc) > tol:
        return False, {
            "reason": "aggregate events_per_sec inconsistent with total_events / wall_clock_sec",
            "reported": eps,
            "recomputed": total / wc,
        }
    summed = 0
    for entry in be.get("per_scenario", []):
        tp = output_dir / entry["sub"] / "trace.parquet"
        if not tp.exists():
            return False, {"reason": f"missing sub trace {entry['sub']}"}
        rows = len(pd.read_parquet(tp))
        if int(entry.get("n_events", -1)) != rows:
            return False, {
                "reason": f"sub {entry['sub']} n_events {entry.get('n_events')} != rows {rows}"
            }
        summed += rows
    if summed != int(total):
        return False, {
            "reason": f"total_events {int(total)} != sum of sub rows {summed}"
        }
    return True, {
        "total_events": int(total),
        "events_per_sec": eps,
        "n_scenarios": len(be.get("per_scenario", [])),
    }
