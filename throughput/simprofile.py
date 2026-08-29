"""SimProfile — the systems-diagnosis special-award artifact (plan §Phase 5; NVIDIA "SimProfile").

A participant may submit, alongside the trace, a component-timed performance profile of their
simulator — where the wall-clock went (matching / event-queue / latency / agent-logic / io / …), plus
GPU utilization and a peak-memory claim. This is a DIAGNOSTIC feeding the "Best Systems Diagnosis"
special award; it is NOT an admissibility gate — a submission is never rejected for an absent or poor
profile. The verifier checks the profile is self-consistent AND consistent with the run's ground-truth
wall-clock + peak memory. Integrity depends on that ground truth being an INDEPENDENT HOST
MEASUREMENT (the harness / ``throughput/timer.py``): a profile checked only against the submission's
own self-reported wall-clock + peak memory proves internal consistency, not integrity, and a
self-consistent fabrication would pass. When the caller has only self-reported ground truth it must
pass ``ground_truth_self_reported=True``, which zeroes the quality so the award cannot rank it.

``profile.json`` schema (a submission output sidecar):
    {
      "components": {"matching": <sec>, "event_queue": <sec>, "latency": <sec>,
                     "agent_logic": <sec>, "io": <sec>, ...},   # per-component wall-clock, seconds
      "gpu_utilization": <float in [0, 1]>,   # mean GPU utilization over the run (0.0 on CPU)
      "peak_memory_bytes": <int>              # the profile's own peak-memory claim
    }
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProfileVerdict:
    """Result of verifying one submitted SimProfile. ``quality`` in ``[0, 1]`` (higher = a more
    granular, self-consistent, accurate profile) feeds the Best-Systems-Diagnosis award; it is 0.0
    for any profile with a breach."""

    valid: bool
    breaches: list[str]
    quality: float


def verify_profile(
    profile: dict[str, Any],
    *,
    wall_clock_sec: float,
    peak_memory_bytes: int,
    tol: float = 0.10,
    ground_truth_self_reported: bool = False,
) -> ProfileVerdict:
    """Verify a submitted profile against the run's ground-truth ``wall_clock_sec`` +
    ``peak_memory_bytes``, which MUST be an independent host measurement. Checks: non-negative
    component times; the components account for the wall-clock (sum within ``tol``);
    ``gpu_utilization`` in ``[0, 1]``; the peak-memory claim is not wildly under-reported vs the
    measured peak. Returns validity + a quality score.

    ``ground_truth_self_reported=True`` means the caller only has the submission's own wall-clock /
    peak-memory, not an independent host measurement. Consistency against self-reported ground truth
    is not integrity — a self-consistent fabricated profile would pass — so quality is forced to 0.0
    and the Best-Systems-Diagnosis award cannot rank it until host measurement is available."""
    if ground_truth_self_reported:
        return ProfileVerdict(
            False, ["ground truth is self-reported; integrity cannot be verified"], 0.0
        )
    breaches: list[str] = []
    components = profile.get("components", {})
    if not isinstance(components, dict) or not components:
        return ProfileVerdict(False, ["profile has no per-component timing"], 0.0)

    try:
        comp_values = [float(v) for v in components.values()]
    except (TypeError, ValueError):
        return ProfileVerdict(False, ["non-numeric component time"], 0.0)
    if any(v < 0 for v in comp_values):
        breaches.append("negative component time")

    total = sum(comp_values)
    consistency = 0.0
    if wall_clock_sec > 0:
        rel = abs(total - wall_clock_sec) / wall_clock_sec
        consistency = max(0.0, 1.0 - rel)
        if rel > tol:
            breaches.append(
                f"components sum {total:.3f}s vs wall_clock {wall_clock_sec:.3f}s (>±{tol:.0%})"
            )

    gpu_util = float(profile.get("gpu_utilization", 0.0))
    if not 0.0 <= gpu_util <= 1.0:
        breaches.append("gpu_utilization outside [0, 1]")

    claimed_mem = int(profile.get("peak_memory_bytes", 0))
    if (
        peak_memory_bytes > 0
        and abs(claimed_mem - peak_memory_bytes) / peak_memory_bytes > 3.0 * tol
    ):
        breaches.append("peak_memory_bytes claim far from measured peak")

    # Quality rewards granularity (how finely the time is attributed, saturating at ~4 components) and
    # consistency with the measured wall-clock; any breach zeroes it (the diagnosis is untrustworthy).
    granularity = min(1.0, len(components) / 4.0)
    quality = 0.0 if breaches else granularity * consistency
    return ProfileVerdict(not breaches, breaches, quality)
