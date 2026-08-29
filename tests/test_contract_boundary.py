"""Track 3 against the Hub's own golden fixtures: the cross-repository boundary, asserted.

Global rule 6 wants an integration or contract test for every cross-repository boundary changed.
Track 3 consumes four Hub artifacts and produces none, so the boundary is entirely about whether
the shipped fixtures say what this track's scorer assumes:

* the **C5 descriptors** must carry ``category: "simulator"`` (ruling R-1). Track 3 supplies no
  fixture of its own -- the Hub publishes one per (track, phase) and CodaBench and the website
  render the same bytes, so a third hand-written example is exactly what the ruling forbids;
* the **C1 simulation plan** must carry ``W = 0.0``, ``direction = desc``, a repeat policy with
  ``every_repeat_must_pass``, and an aggregation block that agrees with the metric;
* the **C2 record** must carry the fields the ranked rate is computed from;
* the **C1 clip ceiling** is the one open item. See :func:`test_the_c1_clip_ceiling_is_either_
  correct_or_the_known_invented_value`.

    python -m pytest tests/test_contract_boundary.py
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import qfbench2_common.contracts as contracts  # noqa: E402
from qfbench2_common.contracts import (  # noqa: E402
    CONTRACT_SET,
    EvaluationPlan,
    RunRecord,
    SubmissionDescriptor,
)

from qfbench2_track_simulation import domain, telemetry  # noqa: E402

FIXTURES = pathlib.Path(contracts.__file__).resolve().parent / "fixtures"

#: The value the shipped C1 simulation fixture carries, recorded so the assertion below can tell
#: "the Hub has not acted on the contract request yet" apart from "the Hub chose a third number".
KNOWN_INVENTED_CEILING = 2_000_000.0


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- C5
def test_every_shipped_simulation_descriptor_declares_the_simulator_category() -> None:
    """R-1. `SUBMISSION_CLI.md` used to say Track 3 sits outside the category enum, while the
    generated wrapper coerced an absent field to `api` -- documented as the CPU tier -- so a Track
    3 submission silently declared CPU while requiring the GPU queue."""
    for phase in ("dev", "final", "verification"):
        raw = _load(f"c5/simulation_{phase}.json")
        descriptor = SubmissionDescriptor.from_mapping(raw)
        assert descriptor.track == "simulation"
        assert descriptor.phase == phase
        assert descriptor.category == "simulator"


def test_a_simulation_descriptor_without_a_category_is_refused() -> None:
    """"Absent means valid" is the fail-open shape the ruling removes."""
    from qfbench2_common.contracts import ContractError

    raw = _load("c5/simulation_final.json")
    del raw["category"]
    try:
        SubmissionDescriptor.from_mapping(raw)
    except ContractError:
        return
    raise AssertionError("a descriptor with no category must not validate")


# --------------------------------------------------------------------------- C1
def test_the_simulation_plan_carries_the_frozen_track_3_policy() -> None:
    plan = EvaluationPlan.from_mapping(_load("c1/simulation_final.expanded.json"))
    assert plan.contract_set == CONTRACT_SET
    assert plan.track == "simulation"
    assert plan.metric.direction == "desc"
    assert plan.metric.domain_min == domain.PARTICIPANT_FAILURE_SCORE == 0.0
    assert plan.participant_failure.score == 0.0, "W is 0.0 events/sec"
    assert plan.participant_failure.clip_real_scores_to_domain is True
    assert plan.organizer_failure_policy == "abort_whole_evaluation"
    assert plan.required_evidence["c2"] is True
    assert plan.required_evidence["telemetry"] is True
    assert plan.every_repeat_must_pass is True
    assert plan.repeats is not None and plan.repeats >= 2
    assert plan.warmup_discarded is not None and plan.warmup_discarded < plan.repeats
    assert plan.is_rankable


def test_the_c1_clip_ceiling_is_either_correct_or_the_known_invented_value() -> None:
    """The one open contract item, written so it stays green in both stable states.

    The shipped fixture carries a ceiling the Hub agent recorded as invented. Track 3's derivation
    (``qfbench2_track_simulation.domain``) puts the minimum at the per-market physical ceiling times
    the widest batch in the roster. Until the Hub lands the change the fixture keeps its value; when
    it does, this test still passes. What it will NOT accept is a third number that is neither
    correct nor the recorded starting point -- that would mean the ceiling had been changed to
    something else without the derivation.
    """
    plan = EvaluationPlan.from_mapping(_load("c1/simulation_final.expanded.json"))
    ceiling = plan.metric.domain_max
    if ceiling >= domain.MAX_PER_MARKET_EVENTS_PER_SEC:
        return  # the contract request has landed
    assert ceiling == KNOWN_INVENTED_CEILING, (
        f"the C1 simulation fixture carries metric.domain.max={ceiling}, which is neither the "
        f"recorded invented value ({KNOWN_INVENTED_CEILING}) nor at or above the derived minimum "
        f"({domain.MAX_PER_MARKET_EVENTS_PER_SEC} per market x the widest batch). See "
        "qfbench2_track_simulation.domain for the derivation."
    )


# --------------------------------------------------------------------------- C2
def test_the_c2_fixture_carries_everything_the_ranked_rate_needs() -> None:
    record = RunRecord.from_mapping(_load("c2_run_record.json"))
    assert record.telemetry is not None
    for field in (
        "sampling_interval_ms",
        "coverage_fraction",
        "gpu_uuid",
        "participant_cgroup_id",
        "exclusive",
        "contender_process_count",
        "throttled",
    ):
        assert field in record.telemetry, field
    assert record.output_row_counts is not None
    assert "sanitized_tree_digest" in record.bindings


def test_the_frozen_telemetry_thresholds_match_c7() -> None:
    """The numbers are frozen in 02A §3 and in C7; Track 3 restates them only as constants that
    the shared helper is called with, so a drift shows up here rather than in a wrong refusal."""
    instance = _load("c7_hardware.json")["telemetry"]
    assert telemetry.SAMPLING_INTERVAL_MS == instance["sampling_interval_ms"] == 50
    assert telemetry.MIN_COVERAGE_FRACTION == instance["min_coverage_fraction"] == 0.95
    assert telemetry.MAX_CONSECUTIVE_MISSED_SAMPLES == instance["max_missed_samples"] == 5


def test_the_simulator_category_exists_in_the_frozen_served_set() -> None:
    """The contract-level requirement: a queue must be able to declare that it serves Track 3."""
    from qfbench2_common.contracts import SERVED_CATEGORIES

    assert "simulator" in SERVED_CATEGORIES


def test_the_golden_c7_instance_does_not_yet_serve_the_simulator_queue() -> None:
    """A recorded gap, not an accusation, and written so it stays green when it is closed.

    R-1 made ``simulator`` a required C5 category for Track 3, and Track 3 runs on its own queue
    with exactly one attached worker. The shipped C7 instance serves the three agent-tier
    categories and not ``simulator``, so there is no golden instance a Track 3 queue can be
    validated against. That is a fixture gap on the Hub side and an open contract request; the
    assertion below flips to the positive case the moment a simulator-serving instance ships.
    """
    served = _load("c7_hardware.json")["served_categories"]
    if "simulator" in served:
        return  # the contract request has landed
    assert served == ["api", "byo-large", "byo-small"], (
        f"the golden C7 instance serves {served}, which is neither the recorded agent-tier set "
        "nor a set including 'simulator'. Track 3's queue cannot be validated against it."
    )


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
