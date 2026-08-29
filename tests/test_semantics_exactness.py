"""Event and trace comparison is EXACT: no intersection, no prefix, no truncation, no NaN.

Four measured defects are pinned closed here. Each test states the pre-fix behaviour it
fails on.

1. **Extras were invisible.** ``check_tier_a`` intersected the two key sets
   (``common = [k for k in rp if k in cp]``), so candidate-only rows never entered the comparison.
   Measured: a candidate with 2.00x the reference's rows passed Tier-A with zero breaches — and
   the padded row count is the ranked numerator.
2. **Ordering was truncated at 50,000 events.** Anything past that index was outside the
   comparison entirely.
3. **Row-count equality was never checked** on the single-unit path. Only the batch path asserted
   it.
4. **Non-finite and non-physical values failed open.** ``check_tier_b`` took ``np.log`` of an
   unguarded mid-price, and ``ks > ceiling`` is ``False`` when ``ks`` is NaN. Measured: an
   all-zero-price candidate and an all-negative-price candidate both passed Tier-B.

    python -m pytest tests/test_semantics_exactness.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from qfbench2_track_simulation import semantics as S  # noqa: E402


def trace(n: int = 400, *, start: int = 0, price0: int = 100_000) -> pd.DataFrame:
    """A minimal Tier-A-comparable trace: alternating quotes and fills at a walking price."""
    idx = list(range(start, start + n))
    return pd.DataFrame(
        {
            "t_ns": [1_000 * i for i in idx],
            "agent_id": [i % 4 for i in idx],
            "msg_type": ["ORDER_FILLED" if i % 2 else "QUOTE_UPDATE" for i in idx],
            "side": ["BID" if i % 2 else "ASK" for i in idx],
            "price": [price0 + (i % 37) for i in idx],
            "size": [10 + (i % 3) for i in idx],
            "order_id": list(idx),
        }
    )


# --------------------------------------------------------------------------- positive control
def test_an_exact_reproduction_passes() -> None:
    ref = trace()
    ok, breaches = S.check_tier_a(ref.copy(), ref)
    assert ok, breaches


def test_an_exact_reproduction_passes_tier_b() -> None:
    ref = trace()
    ok, breaches = S.check_tier_b(ref.copy(), ref)
    assert ok, breaches


def test_semantic_regression_pass_routes_and_passes_the_clean_case() -> None:
    ref = trace()
    for family in (1, 2, 6, 7, 8):
        ok, breaches = S.semantic_regression_pass(ref.copy(), ref, family=family)
        assert ok, (family, breaches)


# --------------------------------------------------------------------------- extras and prefixes
def test_one_unmatched_extra_row_fails() -> None:
    """Faithful trace plus a single unmatched row. Pre-fix: zero breaches."""
    ref = trace()
    extra = pd.DataFrame(
        {
            "t_ns": [10**12],
            "agent_id": [99],
            "msg_type": ["QUOTE_UPDATE"],
            "side": ["ASK"],
            "price": [123_456],
            "size": [7],
            "order_id": [10**9],
        }
    )
    cand = pd.concat([ref, extra], ignore_index=True)
    ok, breaches = S.check_tier_a(cand, ref)
    assert not ok
    assert any("Event count mismatch" in b for b in breaches), breaches
    assert any("coverage is not exact" in b for b in breaches), breaches


def _pad_with_quotes(ref: pd.DataFrame, k: int) -> pd.DataFrame:
    """Append k candidate-only QUOTE_UPDATE rows with fresh identities.

    This is the measured exploit's exact shape: the fill subsequence is untouched, so the fill
    checks are satisfied, and every added row is candidate-only, so the intersection the old
    ordering check ran over never saw them. The rows still count towards the parquet row count,
    which is the ranked numerator.
    """
    last_t = int(ref["t_ns"].iloc[-1])
    extra = pd.DataFrame(
        {
            "t_ns": [last_t + 1_000 * (i + 1) for i in range(k)],
            "agent_id": [7] * k,
            "msg_type": ["QUOTE_UPDATE"] * k,
            "side": ["ASK"] * k,
            "price": [100_500 + i % 11 for i in range(k)],
            "size": [5 + i % 4 for i in range(k)],
            "order_id": [10**6 + i for i in range(k)],
        }
    )
    return pd.concat([ref, extra], ignore_index=True)


def test_a_doubled_trace_fails() -> None:
    """The measured exploit, reproduced exactly: 100% padding with candidate-only non-fill rows,
    well inside the old 50,000-row window. Verified to return ``(True, [])`` on the pre-fix
    module and ``False`` here."""
    ref = trace(1100)
    cand = _pad_with_quotes(ref, 1100)
    assert len(cand) == 2 * len(ref)
    ok, breaches = S.check_tier_a(cand, ref)
    assert not ok, "a candidate with twice the reference's rows must never pass"
    assert any("coverage is not exact" in b for b in breaches), breaches


def test_a_prefix_fails() -> None:
    ref = trace(400)
    ok, breaches = S.check_tier_a(ref.iloc[:200].copy(), ref)
    assert not ok
    assert any("Event count mismatch" in b for b in breaches), breaches


def test_a_subset_with_holes_fails() -> None:
    ref = trace(400)
    cand = ref.drop(index=[7, 42, 300]).reset_index(drop=True)
    ok, breaches = S.check_tier_a(cand, ref)
    assert not ok


def test_a_padded_trace_fails_even_beyond_the_old_truncation_window() -> None:
    """Pre-fix, `_file_order_positions` sliced at 50,000 rows, so a divergence past that index was
    outside the comparison. This candidate is identical for the first 50,000 rows and replaced
    after it."""
    ref = trace(50_400)
    cand = ref.copy()
    # Only non-fill rows are altered, so the fill sequence (which was never truncated) still
    # matches and the ordering check is the only thing that can see the divergence. Verified to
    # return ``(True, [])`` on the pre-fix module.
    beyond = (cand.index >= 50_100) & (cand["msg_type"] == "QUOTE_UPDATE")
    cand.loc[beyond, "agent_id"] = 77
    ok, breaches = S.check_tier_a(cand, ref)
    assert not ok, "a divergence past row 50,000 must be inside the comparison"
    assert any("coverage is not exact" in b for b in breaches), breaches


def test_the_truncation_constant_is_gone() -> None:
    assert not hasattr(S, "KENDALL_MAX_EVENTS"), (
        "the ordering comparison must not have a row window; an attacker chooses what to put "
        "past it"
    )


# --------------------------------------------------------------------------- numeric sanity
def test_nan_prices_fail() -> None:
    ref = trace()
    cand = ref.copy()
    cand.loc[5, "price"] = np.nan
    ok, breaches = S.semantic_regression_pass(cand, ref, family=2)
    assert not ok
    assert any("non-finite" in b for b in breaches), breaches


def test_infinite_sizes_fail() -> None:
    ref = trace()
    cand = ref.copy()
    cand["size"] = cand["size"].astype(float)
    cand.loc[9, "size"] = np.inf
    ok, breaches = S.semantic_regression_pass(cand, ref, family=2)
    assert not ok


def test_zero_priced_stream_fails_tier_b() -> None:
    """Measured pre-fix: `ok=True, breaches=[]`. `np.log(0)` is -inf, the KS came out NaN, and
    `NaN > ceiling` is False."""
    ref = trace()
    cand = ref.copy()
    cand["price"] = 0
    ok, breaches = S.semantic_regression_pass(cand, ref, family=2)
    assert not ok
    assert any("non-positive" in b for b in breaches), breaches


def test_negative_priced_stream_fails_tier_b() -> None:
    ref = trace()
    cand = ref.copy()
    cand["price"] = -100
    ok, breaches = S.semantic_regression_pass(cand, ref, family=2)
    assert not ok


def test_nonfinite_values_fail_tier_a_too() -> None:
    ref = trace()
    cand = ref.copy()
    cand["price"] = cand["price"].astype(float)
    cand.loc[3, "price"] = np.nan
    ok, breaches = S.semantic_regression_pass(cand, ref, family=1)
    assert not ok


def test_a_nonfinite_reference_is_an_organizer_fault() -> None:
    """Non-finite participant DATA is a participant failure; non-finite ORGANIZER material is
    ours and raises, so it can never be recorded as a submission's breach."""
    ref = trace()
    ref = ref.copy()
    ref["price"] = ref["price"].astype(float)
    ref.loc[3, "price"] = np.nan
    try:
        S.semantic_regression_pass(trace(), ref, family=1)
    except S.ReferenceIncomplete:
        return
    raise AssertionError("a non-finite reference must raise ReferenceIncomplete")


def test_an_empty_candidate_trace_fails_rather_than_passing_vacuously() -> None:
    ref = trace()
    ok, breaches = S.semantic_regression_pass(ref.iloc[:0].copy(), ref, family=1)
    assert not ok


# --------------------------------------------------------------------------- card tolerances
#: The lint job checks out WITHOUT lfs and sees pointer stubs, so a test that opens a real
#: `trace.parquet` there dies on `pyarrow.lib.ArrowInvalid` rather than skipping. That is the same
#: split `tests/test_ledger_completeness.py` already handles, and it is handled the same way here:
#: skip where the corpus is genuinely absent, FAIL where the job promised it, so this never
#: becomes a silently skipped check in the job that has the data.
_LFS_POINTER_MAGIC = b"version https://git-lfs.github.com/spec/v1"
_REQUIRE_CORPUS_ENV = "QFB2_T3_REQUIRE_LFS"


def _reference_trace() -> "pathlib.Path":
    import os
    import pathlib as _pl

    import pytest as _pytest

    root = _pl.Path(__file__).resolve().parent.parent
    for candidate in sorted(root.rglob("units/*/trace.parquet")):
        with candidate.open("rb") as fh:
            if fh.read(len(_LFS_POINTER_MAGIC)) != _LFS_POINTER_MAGIC:
                return candidate
    reason = (
        "no materialized units/*/trace.parquet (lfs content absent; only pointer stubs)"
    )
    if os.environ.get(_REQUIRE_CORPUS_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        raise AssertionError(
            f"{reason} -- and {_REQUIRE_CORPUS_ENV} is set, so this check was required to run"
        )
    _pytest.skip(reason)


def test_the_card_timestamp_tolerance_is_honoured() -> None:
    """Pre-fix, `semantic_regression_pass` called `check_tier_a` with no tolerance argument, so the
    official gate always used the 1,000 ns module default while the private oracle and the practice
    harness used the per-scenario value."""
    ref = trace()
    cand = ref.copy()
    cand.loc[cand["msg_type"] == "ORDER_FILLED", "t_ns"] += 4_000
    ok_default, _ = S.semantic_regression_pass(cand, ref, family=1)
    assert not ok_default, "4 us of skew must breach a 1 us tolerance"
    ok_loose, breaches = S.semantic_regression_pass(
        cand, ref, family=1, timestamp_tolerance_ns=5_000
    )
    assert ok_loose, breaches


def test_the_card_kendall_floor_is_honoured() -> None:
    """`kendall_tau_floor` is documented as a per-scenario override and was never read."""
    ref = trace(60)
    shuffled = ref.iloc[list(range(0, 60, 2)) + list(range(1, 60, 2))].reset_index(
        drop=True
    )
    ok_default, breaches = S.check_tier_a(shuffled, ref)
    assert not ok_default, breaches
    ok_loose, _ = S.check_tier_a(shuffled, ref, kendall_tau_floor=-1.0)
    assert (
        ok_loose
    ), "a floor of -1.0 must accept any ordering, proving the parameter is read"


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


def test_a_float_dtype_does_not_break_every_match_key():
    """`astype(str)` made dtype decide identity: str(100) != str(100.0).

    Measured before the fix, on a real unit, casting the four integer key columns to float64 and
    changing no value: "Event coverage is not exact: 14820 reference event(s) absent from the
    candidate and 14820 candidate-only event(s)" -- 100% of keys, reported as a simulation-fidelity
    failure when the traces were identical.

    A pandas round-trip promotes int64 to float64 the moment a NaN appears anywhere in the frame,
    which a merge, a reindex or a groupby can do unprompted. Raised by NVIDIA, issue #48 item 4.
    """
    import pathlib

    import pandas as pd

    from qfbench2_track_simulation.semantics import KENDALL_KEY_COLUMNS, check_tier_a

    ref_path = _reference_trace()
    reference = pd.read_parquet(ref_path)

    candidate = reference.copy()
    promoted = [
        c
        for c in KENDALL_KEY_COLUMNS
        if c in candidate.columns and str(candidate[c].dtype).startswith("int")
    ]
    assert promoted, "fixture must have an integer key column, or this asserts nothing"
    for column in promoted:
        candidate[column] = candidate[column].astype("float64")

    passed, breaches = check_tier_a(candidate, reference)
    assert passed, f"an identical trace was refused for its dtype: {breaches[:1]}"


def test_normalising_the_key_did_not_loosen_it():
    """The companion. A value that genuinely differs must still fail, integral or not."""
    import pathlib

    import pandas as pd

    from qfbench2_track_simulation.semantics import check_tier_a

    ref_path = _reference_trace()
    reference = pd.read_parquet(ref_path)

    changed = reference.copy()
    changed.loc[changed.index[0], "price"] = int(changed["price"].iloc[0]) + 1
    assert not check_tier_a(changed, reference)[
        0
    ], "a changed price must still be caught"

    fractional = reference.copy()
    fractional["price"] = fractional["price"].astype("float64")
    fractional.loc[fractional.index[0], "price"] = (
        float(fractional["price"].iloc[0]) + 0.5
    )
    assert not check_tier_a(
        fractional, reference
    )[
        0
    ], "100.5 must not key as 100 -- the fix normalises integral floats, it does not round"
