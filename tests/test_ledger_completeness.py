"""The message ledger is mandatory by CARD DECLARATION, and truncating it cannot disable the gate.

Two defects meet here.

**The gate triggered on file existence.** ``scoring.py`` ran the message block only when
``unit_dir/message_trace.parquet`` happened to exist, so failing to ship a reference ledger silently
switched the JAX-resistance gate off for that unit. The failure mode was invisible and pointed the
wrong way: the check stopped running rather than complaining. The card decides now, and a card that
requires a ledger with no reference to compare against is an organizer fault.

**The minimum-sample thresholds could disable the gate.** ``check_message_reference`` and
``check_protocol_fidelity`` ran their KS tests only when BOTH sides had at least ten rows, and
``check_message_semantics`` returned ``(True, [])`` on an empty frame. Measured: a 5,000-row ledger
with roughly 500x wrong latencies is rejected, and the same ledger truncated to nine rows passed
both the self-consistency and the reference check with no breaches. The floor is now
reference-relative.

    python -m pytest tests/test_ledger_completeness.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402
import pytest  # noqa: E402

from qfbench2_track_simulation import semantics as S  # noqa: E402
from qfbench2_track_simulation.scoring import _CardPolicy  # noqa: E402

REPO = Path(__file__).resolve().parent.parent

#: First bytes of a git-lfs pointer stub. `units/*.parquet` are lfs-tracked and the lint job checks
#: out WITHOUT lfs deliberately, so there the working tree holds 131-byte pointers rather than
#: parquet. Detecting that is the difference between an honest skip and an ArrowInvalid that reads
#: like a corrupt corpus.
_LFS_POINTER_MAGIC = b"version https://git-lfs.github.com/spec/v1"

#: Set by the CI job that DOES check out lfs. Where the corpus is materialized a skip is a failure,
#: the same rule QFB2_T3_REQUIRE_TOOLKIT enforces in tests/test_unit_cards.py: a check reporting
#: SKIP is not a check that passed.
REQUIRE_CORPUS_ENV = "QFB2_T3_REQUIRE_LFS"


def _corpus_unavailable(reason: str) -> None:
    """Skip, unless this job promised the corpus would be there."""
    if os.environ.get(REQUIRE_CORPUS_ENV, "").strip().lower() in {"1", "true", "yes", "on"}:
        raise AssertionError(
            f"{reason} -- and {REQUIRE_CORPUS_ENV} is set, so this check was required to run"
        )
    pytest.skip(reason)


def _is_lfs_pointer(path: Path) -> bool:
    with path.open("rb") as fh:
        return fh.read(len(_LFS_POINTER_MAGIC)) == _LFS_POINTER_MAGIC


def ledger(
    n: int = 5_000,
    *,
    latency: int = 500,
    break_identity: bool = False,
    duplicate_ids: bool = False,
    broadcast: bool = False,
    fabricate_causal: bool = False,
    wakeups: int = 0,
) -> pd.DataFrame:
    t_send = [1_000 * i for i in range(n)]
    t_recv = [s + latency for s in t_send]
    lat = [latency] * n
    ids = list(range(n))
    parents: list[int | None] = [None] * n
    if break_identity and n:
        lat[n // 2] = latency + 9_999
    dsts = [(i + 1) % 3 for i in range(n)]
    if duplicate_ids and n > 1:
        # A genuine duplicate DELIVERY: the same message arriving twice at the same recipient.
        # Repeating only the id would describe a broadcast, which is legal and is covered by
        # `broadcast=True` below.
        ids[-1] = ids[0]
        dsts[-1] = dsts[0]
    if broadcast and n > 2:
        # One message, several recipients: what ABIDES emits for MarketClosePriceMsg. The id
        # repeats and the recipient does not.
        ids[-1] = ids[-2] = ids[0]
        dsts[-1] = (dsts[0] + 1) % 3
        dsts[-2] = (dsts[0] + 2) % 3
    if fabricate_causal and n > 1:
        # A causal parent that was never delivered to this sender at all.
        parents[-1] = 10**9
    frame = pd.DataFrame(
        {
            "seq": list(range(n)),
            "t_recv_ns": t_recv,
            "t_send_ns": t_send,
            "latency_ns": lat,
            "src_id": [i % 3 for i in range(n)],
            "dst_id": dsts,
            "message_id": ids,
            "msg_type": ["OrderSubmitMsg"] * n,
            "order_id": list(range(n)),
            "causal_parent": parents,
        }
    )
    if wakeups:
        extra = pd.DataFrame(
            {
                "seq": list(range(n, n + wakeups)),
                "t_recv_ns": [10**9 + i for i in range(wakeups)],
                "t_send_ns": [None] * wakeups,
                "latency_ns": [0] * wakeups,
                "src_id": [1] * wakeups,
                "dst_id": [1] * wakeups,
                "message_id": list(range(10**6, 10**6 + wakeups)),
                "msg_type": ["AGENT_WAKEUP"] * wakeups,
                "order_id": [None] * wakeups,
                "causal_parent": [None] * wakeups,
            }
        )
        frame = pd.concat([frame, extra], ignore_index=True)
    return frame


def _empty() -> pd.DataFrame:
    return pd.DataFrame({c: [] for c in S.MESSAGE_TRACE_COLUMNS})


def _expect(exc_type, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc_type as exc:
        return exc
    raise AssertionError(f"expected {exc_type.__name__}, nothing raised")


# --------------------------------------------------------------------------- positive controls
def test_a_faithful_ledger_passes_both_halves() -> None:
    ref = ledger()
    ok, breaches = S.check_message_semantics(ref.copy())
    assert ok, breaches
    ok, breaches = S.check_message_reference(ref.copy(), ref)
    assert ok, breaches


def test_a_wakeup_bearing_ledger_passes() -> None:
    ref = ledger(wakeups=40)
    ok, breaches = S.check_message_semantics(ref.copy())
    assert ok, breaches


# --------------------------------------------------------------------------- the truncation hole
def test_a_truncated_ledger_can_no_longer_skip_the_latency_comparison() -> None:
    """THE measured defect. Nine rows of a wildly wrong latency used to pass both checks."""
    wrong = ledger(9, latency=250_000)
    ok_self, _ = S.check_message_semantics(wrong)
    assert ok_self, "nine self-consistent rows are still self-consistent; that is not the defect"
    ok_ref, breaches = S.check_message_reference(wrong, ledger())
    assert not ok_ref, "a 9-row ledger against a 5,000-row reference must not pass"
    assert any("reference-relative floor" in b for b in breaches), breaches


def test_the_full_length_wrong_latency_ledger_still_fails() -> None:
    ok, breaches = S.check_message_reference(ledger(latency=250_000), ledger())
    assert not ok
    assert any("Realized-latency" in b for b in breaches), breaches


def test_a_ledger_just_under_the_relative_floor_fails_and_just_over_passes() -> None:
    ref = ledger(1_000)
    under, over = ledger(899), ledger(901)
    assert not S.check_message_reference(under, ref)[0]
    assert S.check_message_reference(over, ref)[0]


# --------------------------------------------------------------------------- empty / malformed
def test_an_empty_candidate_ledger_fails_self_consistency() -> None:
    """Pre-fix: `(True, [])`. A zero-row ledger evaded every latency, causal and wakeup check."""
    ok, breaches = S.check_message_semantics(_empty())
    assert not ok
    assert any("no rows" in b for b in breaches), breaches


def test_a_duplicate_delivery_fails() -> None:
    """The same message arriving twice at the same recipient is not a thing the kernel does."""
    ok, breaches = S.check_message_semantics(ledger(200, duplicate_ids=True))
    assert not ok
    assert any("not unique" in b for b in breaches), breaches


def test_a_broadcast_passes() -> None:
    """One message, several recipients, one `message_id`. This is what ABIDES emits.

    Requiring `message_id` alone to be unique rejected it, and therefore rejected every shipped
    reference ledger: measured, 59 of 65 units refused a byte-for-byte perfect submission with
    `t3.latency_causality_violation`. The invariant is the pair `(message_id, dst_id)`, which is
    also the key `recv_by_key` is built on, so the id alone was always stricter than the lookup it
    was introduced to protect.
    """
    ok, breaches = S.check_message_semantics(ledger(200, broadcast=True))
    assert ok, breaches


def test_every_shipped_reference_ledger_passes_self_consistency() -> None:
    """The acceptance test. A perfect submission emits the reference; the gate must admit it.

    Reads the shipped corpus, so it needs `lfs: true` at checkout. All-or-nothing on purpose: a
    partly fetched corpus would silently narrow the acceptance evidence to whichever units happened
    to arrive, and report green for it.
    """
    # The EXPECTED set comes from the cards, not from what happens to be on disk. `if p.exists()`
    # silently narrowed the evidence to whichever ledgers had arrived: measured, renaming 63 of
    # them away left this test reporting `1 passed` in 1.17s against 8.34s for the real run --
    # green on 3% coverage, which is exactly what the docstring above says is not allowed to
    # happen. The lfs-pointer check below catches a STUB; it cannot catch an absence.
    #
    # `_CardPolicy` is the production resolver: `requires_message_ledger` is declared in the card
    # when present and otherwise derived from the scenario family, so asking it here means the
    # acceptance set is the same one the scorer will demand. Measured today: 59 units require a
    # ledger, 13 do not.
    from qfbench2_track_simulation.scoring import _CardPolicy

    required = sorted(
        u for u in (REPO / "units").iterdir()
        if u.is_dir() and (u / "card.toml").exists()
        and _CardPolicy(u).requires_message_ledger
    )
    if not required:
        _corpus_unavailable("no unit card requires a message ledger")

    absent = [u.name for u in required if not (u / "message_trace.parquet").exists()]
    if absent:
        _corpus_unavailable(
            f"{len(absent)}/{len(required)} required reference ledgers are absent from the "
            f"working tree (first: {absent[:3]})"
        )
    ledgers = sorted(u / "message_trace.parquet" for u in required)
    unfetched = [p.parent.name for p in ledgers if _is_lfs_pointer(p)]
    if unfetched:
        _corpus_unavailable(
            f"{len(unfetched)}/{len(ledgers)} reference ledgers are unfetched git-lfs pointers"
        )
    breached = [
        p.parent.name for p in ledgers if not S.check_message_semantics(pd.read_parquet(p))[0]
    ]
    assert not breached, f"reference ledgers rejected by the candidate gate: {breached}"


def test_a_fabricated_causal_parent_fails() -> None:
    ok, breaches = S.check_message_semantics(ledger(200, fabricate_causal=True))
    assert not ok
    assert any("Dangling causal_parent" in b for b in breaches), breaches


def test_a_broken_latency_identity_fails() -> None:
    ok, breaches = S.check_message_semantics(ledger(200, break_identity=True))
    assert not ok
    assert any("Latency identity" in b for b in breaches), breaches


def test_a_missing_column_fails_rather_than_raising() -> None:
    frame = ledger(50).drop(columns=["causal_parent"])
    ok, breaches = S.check_message_semantics(frame)
    assert not ok
    assert any("missing columns" in b for b in breaches), breaches


# --------------------------------------------------------------------------- organizer faults
def test_an_empty_reference_ledger_is_an_organizer_fault() -> None:
    """Pre-fix, an empty reference returned `(True, [])` — 'no reference ledger to compare
    against, nothing to enforce'. By the time this is reached the card has declared the comparison
    mandatory, so a reference that cannot support it is ours."""
    exc = _expect(S.ReferenceIncomplete, S.check_message_reference, ledger(), _empty())
    assert "empty" in str(exc)


def test_a_reference_with_no_sent_messages_is_an_organizer_fault() -> None:
    ref = ledger(0, wakeups=20)
    _expect(S.ReferenceIncomplete, S.check_message_reference, ledger(), ref)


def test_protocol_fidelity_refuses_a_reference_with_no_exec_reports() -> None:
    _expect(S.ReferenceIncomplete, S.check_protocol_fidelity, ledger(), ledger())


# --------------------------------------------------------------------------- card declaration
def _card(tmp: Path, family: str, extra: str = "") -> Path:
    unit = tmp / "unit"
    unit.mkdir(parents=True, exist_ok=True)
    (unit / "card.toml").write_text(
        "schema_version = \"2.0\"\n"
        "[task]\n"
        f"scenario_family = \"{family}\"\n"
        "[scoring.params]\n"
        "semantic_tier = \"A\"\n" + extra
    )
    return unit


def test_the_card_declares_whether_a_ledger_is_required(tmp_path: Path) -> None:
    """The declaration, not the presence of a file, decides."""
    assert _CardPolicy(_card(tmp_path / "a", "matching-engine-semantics")).requires_message_ledger
    assert _CardPolicy(_card(tmp_path / "b", "latency-profile")).requires_message_ledger
    assert _CardPolicy(_card(tmp_path / "c", "exchange-protocol")).requires_message_ledger
    assert _CardPolicy(_card(tmp_path / "d", "reactive-agent")).requires_message_ledger
    # throughput-scale is the one family whose single-market units do not ship one.
    assert not _CardPolicy(_card(tmp_path / "e", "throughput-scale")).requires_message_ledger


def test_an_explicit_card_declaration_overrides_the_family_default(tmp_path: Path) -> None:
    required = _card(
        tmp_path / "f", "throughput-scale", "requires_message_ledger = true\n"
    )
    assert _CardPolicy(required).requires_message_ledger
    waived = _card(
        tmp_path / "g", "matching-engine-semantics", "requires_message_ledger = false\n"
    )
    assert not _CardPolicy(waived).requires_message_ledger


def test_an_unrecognised_family_defaults_to_requiring_the_ledger(tmp_path: Path) -> None:
    """Fail-closed direction: when we do not know, ask for the evidence."""
    policy = _CardPolicy(_card(tmp_path / "h", "not-a-real-family"))
    assert policy.family == 0
    assert policy.requires_message_ledger


def test_card_tolerances_are_read_from_the_card(tmp_path: Path) -> None:
    unit = _card(
        tmp_path / "i",
        "latency-profile",
        "timestamp_tolerance_ns = 5000\nkendall_tau_floor = 0.95\n",
    )
    policy = _CardPolicy(unit)
    assert policy.timestamp_tolerance_ns == 5_000
    assert policy.kendall_tau_floor == 0.95


def _run_all() -> int:
    import tempfile

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            if "tmp_path" in t.__code__.co_varnames[: t.__code__.co_argcount]:
                with tempfile.TemporaryDirectory() as tmp:
                    t(Path(tmp))
            else:
                t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
