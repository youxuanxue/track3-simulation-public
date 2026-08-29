"""Phase-4 secondary throughput diagnostics — reported, not ranked (plan §5.3).

Pure functions over harness-measured run telemetry (from ``events.json``: ``events_per_sec``,
``n_events``, ``wall_clock_sec``, ``peak_memory_bytes``, ``gpu_seconds``) plus the CPU-ABIDES
baseline throughput and the card ``[environment]``. These three numbers contextualize a result and
feed the special awards; they NEVER re-order the events/sec leaderboard (ranking stays raw median
events/sec, per ``baselines/README.md`` and plan §5.3). They are computed offline (in
``final_scorer``) alongside the platform's live events/sec path, so nothing here touches the shared
runner or the primary metric.

Definitions (plan §5.3, DIRECTIONS NOTE §5):
  * speedup           = submission events/sec ÷ CPU-ABIDES baseline events/sec (same fixed-SKU box)
  * efficiency        = events/sec ÷ GPU-hours when the run actually used the device
                        (``gpu_seconds > 0``), else ÷ CPU-core-hours (``cpus × wall_clock_sec``).
                        Every Track-3 card declares ``[environment] gpu = true`` — a GPU is attached
                        to every timed run and using it is optional — so the card flag no longer
                        splits "GPU units" from "CPU units"; ``gpu_seconds > 0`` is the discriminator
  * memory efficiency = events processed ÷ peak resident memory (events / byte; higher = more compact)
  * gpu utilization   = measured GPU busy time ÷ wall clock. NOT a ranked diagnostic: it is the
                        eligibility signal for the GPU award, separating "the device was attached"
                        from "the device was used". See ``GPU_UTILIZATION_FLOOR``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

#: Minimum measured ``gpu_seconds / wall_clock_sec`` for a run to count as having USED the device.
#:
#: Award eligibility used to be ``gpu_seconds > 0``, which any submission satisfies by issuing one
#: trivial kernel. The gap between that and genuine use is about three orders of magnitude — a
#: measured token memcpy sits near 0.0003 while a real GPU port runs an order 0.5-0.9 — so the floor
#: is set well below any plausible genuine port rather than tuned. It is an eligibility gate ONLY:
#: nothing is ranked on utilization, because a metric that rewards higher utilization would just
#: invert the old defect and pay submissions to keep the device pointlessly busy.
GPU_UTILIZATION_FLOOR = 0.05


@dataclass(frozen=True)
class Diagnostics:
    """The three secondary diagnostics for one unit run. Any field may be ``None`` when its input is
    unavailable (e.g. no baseline throughput, or zero-valued telemetry)."""

    speedup_vs_cpu_abides: float | None
    efficiency: float | None
    efficiency_unit: str  # "events_per_gpu_hour" | "events_per_cpu_core_hour"
    memory_efficiency: float | None  # events per byte of peak resident memory
    # Provenance of the telemetry these numbers were derived from (``gpu_seconds`` +
    # ``peak_memory_bytes``). True when they came from the submission's own ``events.json`` rather
    # than an independent host measurement. This matters more now that award eligibility keys on
    # ``gpu_seconds > 0``: a self-reported gpu_seconds decides both eligibility AND the efficiency
    # it is ranked on, so a submission could fabricate its way onto the award with a tiny non-zero
    # value. The telemetry-dependent awards refuse to rank an entry flagged here.
    telemetry_self_reported: bool = True
    #: Measured GPU busy time as a fraction of wall clock, or ``None`` when either input is missing.
    #: Reported for context and used as the GPU award's eligibility test; never ranked on.
    gpu_utilization: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute(
    *,
    events_per_sec: float,
    n_events: int,
    wall_clock_sec: float,
    peak_memory_bytes: int | None,
    gpu_seconds: float | None,
    baseline_events_per_sec: float | None,
    cpus: float,
    gpu: bool,
    telemetry_self_reported: bool = True,
) -> Diagnostics:
    """Compute the three diagnostics for a single admissible unit run.

    ``telemetry_self_reported`` records where ``gpu_seconds`` / ``peak_memory_bytes`` came from: pass
    ``False`` only when they are independent host measurements. It defaults to ``True`` so a caller
    that does not supply host telemetry is conservatively marked self-reported and cannot win a
    telemetry-dependent award.

    ``events_per_sec`` should be the harness-measured (or, interim, self-reported) throughput; the
    others come straight from ``events.json`` and the card ``[environment]``. Efficiency is billed
    in GPU-hours only when the run actually used the device (``gpu_seconds > 0``), else in
    CPU-core-hours. ``gpu`` is the card flag, which is ``true`` on every Track-3 card and therefore
    no longer discriminates; it is kept only as a guard for non-Track-3 callers.
    """
    speedup: float | None = None
    if baseline_events_per_sec and baseline_events_per_sec > 0:
        speedup = float(events_per_sec) / float(baseline_events_per_sec)

    efficiency: float | None
    # Keyed on gpu_seconds > 0 — did this run actually use the device? The card flag is true for
    # every Track-3 unit, so it cannot separate GPU from CPU submissions on its own.
    if gpu and gpu_seconds and gpu_seconds > 0:
        efficiency = float(events_per_sec) / (float(gpu_seconds) / 3600.0)
        efficiency_unit = "events_per_gpu_hour"
    elif wall_clock_sec and wall_clock_sec > 0 and cpus > 0:
        # CPU-core-hours = cpus × wall-clock, so a submission that wins throughput by burning more
        # cores is penalized on efficiency exactly as one that burns more GPU-hours would be.
        efficiency = float(events_per_sec) / (
            float(cpus) * float(wall_clock_sec) / 3600.0
        )
        efficiency_unit = "events_per_cpu_core_hour"
    else:
        efficiency = None
        efficiency_unit = "events_per_cpu_core_hour"

    memory_efficiency: float | None = None
    if peak_memory_bytes and peak_memory_bytes > 0:
        memory_efficiency = float(n_events) / float(peak_memory_bytes)

    # Did the run USE the device, or merely have one attached? gpu_seconds alone cannot say: it is
    # an absolute quantity, so a long run that barely touched the GPU and a short one that saturated
    # it can report the same figure. Dividing by the wall clock makes it a fraction that a token
    # kernel cannot fake without actually occupying the device.
    gpu_utilization: float | None = None
    if gpu_seconds is not None and wall_clock_sec and wall_clock_sec > 0:
        gpu_utilization = float(gpu_seconds) / float(wall_clock_sec)

    return Diagnostics(
        speedup,
        efficiency,
        efficiency_unit,
        memory_efficiency,
        telemetry_self_reported,
        gpu_utilization,
    )
