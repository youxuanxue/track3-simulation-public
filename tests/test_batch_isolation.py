"""Tests for the BatchMarketSim isolation gate's message-ledger enforcement.

The ledger is checked in two halves with different requirements, and the split matters:

  * self-consistency (``check_message_semantics``) is CANDIDATE-ONLY — latency identity, contiguous
    0..N-1 seq permutation, causal produced-before-consumed ordering, wakeup structure. It needs no
    reference, so it must run on EVERY sub. This is the half carrying the causality invariant the
    batch family exists to defend.
  * reference-equivalence (``check_message_reference``) and family-7 protocol fidelity compare
    against the reference ledger, so they run where the sub's ``batch.json`` entry DECLARES one
    (``reference_message_sha256``).

Both were previously gated behind the reference ledger existing, so a sub whose reference shipped no
ledger was not ledger-checked at all. These tests pin the corrected behaviour, which is also what
allows a wide batch to ship reference ledgers for a sample of its subs rather than all of them.

The trigger is now the DECLARATION, not the file. Discovering the sample by asking whether a file
happens to exist means deleting a reference ledger silently switches the gate off, and the failure
points the wrong way -- the check stops running instead of complaining. A sub that declares a
reference ledger and does not have one raises ``ReferenceIncomplete``, which the gate turns into a
whole-evaluation organizer fault.

    python tests/test_batch_isolation.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from qfbench2_track_simulation.batch import score_subs  # noqa: E402
from qfbench2_track_simulation.semantics import ReferenceIncomplete  # noqa: E402

#: Any non-empty value: the declaration is what selects the gate, the digest itself is verified by
#: the bundle builder rather than by the isolation gate.
DECLARED_SHA = "0" * 64

CEILINGS = {"ks": 0.08, "acf_abs_l2": 0.12, "hill_abs": 1.5, "depth_js": 0.10}


def _trace(n: int = 40) -> pd.DataFrame:
    """A minimal but Tier-A-comparable trace: alternating fills at a walking price."""
    return pd.DataFrame(
        {
            "t_ns": [1_000 * i for i in range(n)],
            "agent_id": [i % 4 for i in range(n)],
            "msg_type": ["ORDER_FILLED" if i % 2 else "QUOTE_UPDATE" for i in range(n)],
            "side": ["BID" if i % 2 else "ASK" for i in range(n)],
            "price": [100_000 + (i % 7) for i in range(n)],
            "size": [10 + (i % 3) for i in range(n)],
            "order_id": list(range(n)),
        }
    )


def _ledger(n: int = 20, *, latency: int = 500, break_identity: bool = False) -> pd.DataFrame:
    """A self-consistent message ledger, or one whose latency column contradicts its timestamps."""
    t_send = [1_000 * i for i in range(n)]
    t_recv = [s + latency for s in t_send]
    lat = [latency] * n
    if break_identity:
        # t_recv - t_send no longer equals latency_ns: the shape a port gets when it fabricates the
        # latency column rather than actually modelling per-message delay.
        lat[n // 2] = latency + 9_999
    return pd.DataFrame(
        {
            "seq": list(range(n)),
            "t_recv_ns": t_recv,
            "t_send_ns": t_send,
            "latency_ns": lat,
            "src_id": [i % 3 for i in range(n)],
            "dst_id": [(i + 1) % 3 for i in range(n)],
            "message_id": list(range(n)),
            "msg_type": ["OrderSubmitMsg"] * n,
            "order_id": list(range(n)),
            "causal_parent": [None] * n,
        }
    )


def _unit(root: Path, *, ship_ref_ledger: bool, cand_ledger: pd.DataFrame | None,
          cand_trace: pd.DataFrame | None = None,
          declare_ref_ledger: bool | None = None) -> tuple[list[dict], Path, Path]:
    """Build a one-sub batch unit plus a candidate output tree.

    ``declare_ref_ledger`` defaults to ``ship_ref_ledger``: an honest unit declares exactly what it
    ships. Passing them apart is how the two organizer-fault cases below are built.
    """
    refs, out = root / "checks" / "reference_data", root / "out"
    (refs / "sub_00").mkdir(parents=True)
    (out / "sub_00").mkdir(parents=True)
    ref_trace = _trace()
    ref_trace.to_parquet(refs / "sub_00" / "trace.parquet")
    (cand_trace if cand_trace is not None else ref_trace).to_parquet(
        out / "sub_00" / "trace.parquet"
    )
    if ship_ref_ledger:
        _ledger().to_parquet(refs / "sub_00" / "message_trace.parquet")
    if cand_ledger is not None:
        cand_ledger.to_parquet(out / "sub_00" / "message_trace.parquet")
    declares = ship_ref_ledger if declare_ref_ledger is None else declare_ref_ledger
    entry: dict = {"sub": "sub_00"}
    if declares:
        entry["reference_message_sha256"] = DECLARED_SHA
    return [entry], refs, out


def _score(subs, refs, out):
    return score_subs(subs, refs, out, family=6, tier="A", ceilings=CEILINGS)


def test_clean_batch_passes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        subs, refs, out = _unit(Path(tmp), ship_ref_ledger=True, cand_ledger=_ledger())
        ok, failures = _score(subs, refs, out)
        assert ok, failures


def test_selfcheck_runs_even_when_no_reference_ledger_ships() -> None:
    # The regression this file exists for. The reference ships no ledger, so reference-equivalence
    # cannot run — but the candidate's ledger contradicts itself, and that must still be caught.
    # Before the split, this sub was not ledger-checked at all and the batch passed.
    with tempfile.TemporaryDirectory() as tmp:
        subs, refs, out = _unit(
            Path(tmp), ship_ref_ledger=False, cand_ledger=_ledger(break_identity=True)
        )
        ok, failures = _score(subs, refs, out)
        assert not ok, "a self-inconsistent candidate ledger must fail without a reference ledger"
        assert failures[0]["isolation"] == "message_semantics", failures


def test_candidate_ledger_is_required_even_when_no_reference_ledger_ships() -> None:
    # message_trace.parquet is a required output for every batch sub (README "Submission format").
    # A sub that omits it cannot demonstrate the latency-causality semantics, so it fails whether or
    # not the reference happens to carry a ledger of its own.
    with tempfile.TemporaryDirectory() as tmp:
        subs, refs, out = _unit(Path(tmp), ship_ref_ledger=False, cand_ledger=None)
        ok, failures = _score(subs, refs, out)
        assert not ok
        assert "missing candidate message_trace.parquet" in failures[0]["reason"], failures


def test_reference_equivalence_only_applies_where_a_reference_ledger_is_declared() -> None:
    # A candidate ledger that is internally consistent but has a different realized-latency
    # distribution fails ONLY where the sub DECLARES a reference ledger. Where none is declared the
    # sub is held to self-consistency alone — which is what makes ledger sampling sound.
    divergent = _ledger(latency=90_000)  # self-consistent, but nothing like the reference's 500 ns
    with tempfile.TemporaryDirectory() as tmp:
        subs, refs, out = _unit(Path(tmp), ship_ref_ledger=True, cand_ledger=divergent)
        ok, failures = _score(subs, refs, out)
        assert not ok, "latency divergence must fail where a reference ledger ships"
        assert failures[0]["isolation"] == "message_reference", failures
    with tempfile.TemporaryDirectory() as tmp:
        subs, refs, out = _unit(Path(tmp), ship_ref_ledger=False, cand_ledger=divergent)
        ok, failures = _score(subs, refs, out)
        assert ok, f"without a declared reference ledger only self-consistency applies: {failures}"


def test_declared_reference_ledger_that_is_missing_is_an_organizer_fault() -> None:
    # THE regression. Before the declaration rule, deleting
    # checks/reference_data/<sub>/message_trace.parquet
    # silently disabled reference-equivalence for that sub and the batch passed. It is now our
    # fault, raised rather than returned, so the caller cannot record it as a participant verdict.
    divergent = _ledger(latency=90_000)
    with tempfile.TemporaryDirectory() as tmp:
        subs, refs, out = _unit(
            Path(tmp), ship_ref_ledger=False, cand_ledger=divergent, declare_ref_ledger=True
        )
        try:
            _score(subs, refs, out)
        except ReferenceIncomplete as exc:
            assert "declares reference_message_sha256" in str(exc), exc
        else:  # pragma: no cover - the assertion below reports it
            raise AssertionError("a declared-but-absent reference ledger must raise")


def test_missing_reference_trace_is_an_organizer_fault() -> None:
    # Same principle one level up: a sub with no reference trace at all cannot be compared, and
    # "no reference" must never read as "nothing to enforce".
    with tempfile.TemporaryDirectory() as tmp:
        subs, refs, out = _unit(Path(tmp), ship_ref_ledger=True, cand_ledger=_ledger())
        (refs / "sub_00" / "trace.parquet").unlink()
        try:
            _score(subs, refs, out)
        except ReferenceIncomplete as exc:
            assert "no reference trace" in str(exc), exc
        else:  # pragma: no cover
            raise AssertionError("a missing reference trace must raise")


def test_a_divergent_trace_still_fails_before_the_ledger_is_reached() -> None:
    # The ledger split must not have weakened the Tier-A isolation check that precedes it.
    with tempfile.TemporaryDirectory() as tmp:
        bad = _trace()
        bad.loc[1, "price"] = 999_999
        subs, refs, out = _unit(
            Path(tmp), ship_ref_ledger=True, cand_ledger=_ledger(), cand_trace=bad
        )
        ok, failures = _score(subs, refs, out)
        assert not ok
        assert failures[0]["isolation"] == "semantic", failures


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
