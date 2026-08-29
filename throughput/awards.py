"""Four special awards (plan §3.2, §5.3; NVIDIA-aligned, sponsor-facing).

Each award is a RANKING over one diagnostic, computed offline across the admissible submissions —
it sits BESIDE the primary events/sec leaderboard and never re-orders it. The selection logic here is
track-owned and pure; gathering the cross-submission input and displaying the winners is offline /
platform-publisher territory.

  * Best GPU Acceleration        — highest speedup over the CPU-ABIDES baseline among submissions
                                   that measurably USED the device (see ``best_gpu_acceleration``)
  * Best Speed–Realism Frontier  — the frontier point with the best balanced speed×realism score
  * Best Latency-Semantics Preservation — tightest latency-causality margin (most faithful kernel timing)
  * Best Systems Diagnosis       — best SimProfile quality (Phase 5)
"""

from __future__ import annotations

from dataclasses import dataclass

from .diagnostics import GPU_UTILIZATION_FLOOR, Diagnostics
from .frontier import FrontierPoint, pareto_frontier


@dataclass(frozen=True)
class AwardEntry:
    """One admissible submission's award-relevant summary. Any field may be ``None`` when the
    submission did not produce that signal (e.g. a submission that never touched the device has no
    GPU-hour ``efficiency``; a submission without a SimProfile has no ``systems_diagnosis_score``)."""

    label: str
    efficiency: float | None = (
        None  # events/sec per GPU-hour; set only when gpu_seconds > 0
    )
    # Did the run actually use the device (gpu_seconds > 0)? This is NOT the card's
    # ``[environment] gpu`` flag: every Track-3 card declares gpu = true, so the flag is constant
    # and would make every submission eligible. Populate this from the run's gpu_seconds telemetry.
    is_gpu: bool = False
    frontier_point: FrontierPoint | None = None
    latency_margin: float | None = (
        None  # higher = kernel latency/causality more faithfully preserved
    )
    systems_diagnosis_score: float | None = None  # SimProfile quality (Phase 5)
    # True when this entry's GPU-time / peak-memory telemetry is self-reported rather than an
    # independent host measurement. Since eligibility keys on ``gpu_seconds > 0``, a self-reported
    # value decides BOTH whether the entry is eligible and the efficiency it is ranked on, so a
    # submission could fabricate a tiny non-zero gpu_seconds and win. The two telemetry-dependent
    # awards refuse to rank such an entry.
    #
    # The default is deliberately FAIL-CLOSED. The cross-submission assembler that builds these
    # entries lives outside this repo, so an assembler that predates this guard, or that simply does
    # not thread the field, must lose the award rather than silently win it.
    telemetry_self_reported: bool = True
    # ``Diagnostics.efficiency_unit`` carried through, so a GPU-hour number is never compared with a
    # CPU-core-hour one. ``None`` from an assembler that does not set it is ineligible.
    efficiency_unit: str | None = None
    # What Best GPU Acceleration ranks on: throughput over the CPU-ABIDES baseline. Unlike
    # ``efficiency`` it contains no ``gpu_seconds``, so there is nothing for a submission to
    # minimise. Fail-closed: ``None`` is ineligible.
    speedup_vs_cpu_abides: float | None = None
    # Measured GPU busy time / wall clock. ELIGIBILITY ONLY — never ranked on, because rewarding
    # higher utilization would simply invert the old defect and pay submissions to keep the device
    # pointlessly busy. Fail-closed: ``None`` is ineligible.
    gpu_utilization: float | None = None


def entry_from_diagnostics(
    label: str,
    diagnostics: Diagnostics,
    *,
    is_gpu: bool,
    frontier_point: FrontierPoint | None = None,
    latency_margin: float | None = None,
    systems_diagnosis_score: float | None = None,
) -> AwardEntry:
    """Build an :class:`AwardEntry` from a submission's aggregated :class:`Diagnostics`.

    The cross-submission assembler lives outside this repository, which makes the provenance fields
    the easiest thing to get wrong: an assembler that forgets ``telemetry_self_reported`` or
    ``efficiency_unit`` produces an entry that is silently ineligible. Constructing entries through
    this function carries both across from the diagnostics that produced them. ``is_gpu`` stays an
    explicit argument because it is a property of the run (``gpu_seconds > 0``), not of the
    diagnostics record.
    """
    return AwardEntry(
        label=label,
        efficiency=diagnostics.efficiency,
        efficiency_unit=diagnostics.efficiency_unit,
        speedup_vs_cpu_abides=diagnostics.speedup_vs_cpu_abides,
        gpu_utilization=diagnostics.gpu_utilization,
        is_gpu=is_gpu,
        telemetry_self_reported=diagnostics.telemetry_self_reported,
        frontier_point=frontier_point,
        latency_margin=latency_margin,
        systems_diagnosis_score=systems_diagnosis_score,
    )


def best_gpu_acceleration(entries: list[AwardEntry]) -> str | None:
    """Highest speedup over the CPU-ABIDES baseline, among submissions that measurably used the GPU.

    **Why not events per GPU-hour.** That was the original basis and it is inverted: ``efficiency =
    events_per_sec / (gpu_seconds / 3600)`` puts GPU time in the DENOMINATOR while eligibility was
    only ``gpu_seconds > 0``, so the metric decreases monotonically in GPU use and the award goes to
    whoever touches the device least. Measured through this module: a CPU simulator issuing a single
    tiny memcpy beat a saturated genuine port by ~3,700x. Host-side measurement does not fix it —
    those gpu_seconds are exactly what NVML honestly reports; the winner is not lying.

    A utilization floor alone does not fix it either. Because ``gpu_seconds`` stays in the
    denominator, a floor merely relocates the optimum to itself: a submission running a trivial
    kernel to just over the threshold still outranks a real port. The ranked quantity has to change.

    So eligibility and ranking are separated:

    * **eligible** — the run used the device (``gpu_utilization >= GPU_UTILIZATION_FLOOR``), on
      host-measured telemetry. Utilization rather than raw ``gpu_seconds`` because the latter is
      absolute: a long run that barely touched the GPU reports the same figure as a short one that
      saturated it.
    * **ranked** — by ``speedup_vs_cpu_abides``. It contains no ``gpu_seconds``, so there is nothing
      to minimise, and it says what the award's name claims to: how much faster than the CPU
      reference this submission made the simulation. Utilization is never ranked on, which would
      only invert the defect the other way and pay submissions to keep the device busy for its own
      sake.

    ``efficiency`` remains a reported diagnostic — it orders correctly among submissions at
    comparable utilization — but it is no longer what decides the award.
    """
    gpu = [
        e
        for e in entries
        if e.is_gpu
        and not e.telemetry_self_reported
        and e.gpu_utilization is not None
        and e.gpu_utilization >= GPU_UTILIZATION_FLOOR
        and e.speedup_vs_cpu_abides is not None
    ]
    return max(gpu, key=lambda e: e.speedup_vs_cpu_abides).label if gpu else None  # type: ignore[arg-type,return-value]


def best_speed_realism_frontier(entries: list[AwardEntry]) -> str | None:
    points: list[FrontierPoint] = [
        e.frontier_point for e in entries if e.frontier_point is not None
    ]
    front = pareto_frontier(points)
    if not front:
        return None
    # Among the Pareto-optimal, the winner maximizes a balanced speed×realism product so neither a
    # fast-but-unrealistic nor a realistic-but-slow submission runs away with it.
    winner: FrontierPoint = max(front, key=lambda p: p.throughput * p.realism)
    return winner.label


def best_latency_semantics_preservation(entries: list[AwardEntry]) -> str | None:
    cand = [e for e in entries if e.latency_margin is not None]
    return max(cand, key=lambda e: e.latency_margin).label if cand else None  # type: ignore[arg-type,return-value]


def best_systems_diagnosis(entries: list[AwardEntry]) -> str | None:
    # SimProfile quality is only trustworthy when the profile was verified against host-measured
    # ground truth (see ``simprofile.verify_profile``); exclude self-reported entries so a
    # self-consistent fabricated profile cannot win.
    cand = [
        e
        for e in entries
        if e.systems_diagnosis_score is not None and not e.telemetry_self_reported
    ]
    return max(cand, key=lambda e: e.systems_diagnosis_score).label if cand else None  # type: ignore[arg-type,return-value]


def select_all(entries: list[AwardEntry]) -> dict[str, str | None]:
    """Compute all four award winners (label or ``None`` if no eligible submission)."""
    return {
        "best_gpu_acceleration": best_gpu_acceleration(entries),
        "best_speed_realism_frontier": best_speed_realism_frontier(entries),
        "best_latency_semantics_preservation": best_latency_semantics_preservation(
            entries
        ),
        "best_systems_diagnosis": best_systems_diagnosis(entries),
    }
