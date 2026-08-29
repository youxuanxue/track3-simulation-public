"""Offline Phase-4 secondary-diagnostics report for a Track-3 submission (plan §5.3).

Consumes a submission's per-unit ``events.json`` (with the ``peak_memory_bytes`` / ``gpu_seconds``
telemetry) plus the matching reference ``events.json`` (whose ``events_per_sec`` is the CPU-ABIDES
baseline throughput for that unit), and produces the three secondary diagnostics per unit and their
medians across the submission. This is emitted beside the platform's live events/sec leaderboard — it
never re-orders the primary ranking (§5.3). Frontier points + award entries are assembled by
``throughput.frontier`` / ``throughput.awards`` across submissions once each submission's report and
stylized-fact divergence are in hand.

Usage:
    python -m throughput.report --submission <out_dir> --reference <ref_dir> --out report.json
where both dirs hold one ``<unit>/events.json`` per unit (matched by subdirectory name); when a
``<unit>/card.toml`` is absent (or omits the keys) the card ``[environment]`` cpus/gpu fall back to
the frozen Track-3 container contract — 4 / true — never to a CPU-shaped guess.
"""

from __future__ import annotations

import argparse
import json
import statistics
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .diagnostics import Diagnostics, compute

# Frozen Track-3 container contract: every timed unit runs with cpus = 4 and a GPU attached, so
# every Track-3 card declares ``[environment] gpu = true``. These are the no-card fallbacks — the
# contract itself, not a guess. Defaulting ``gpu`` to false here would silently reclassify a run
# that really used the device as a CPU run and discard its GPU accounting. The fallback cannot
# invent GPU accounting in the other direction either: ``diagnostics.compute`` still requires
# ``gpu_seconds > 0`` before it bills anything in GPU-hours.
CONTRACT_CPUS = 4.0
CONTRACT_GPU = True


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _host_get(host: dict[str, Any], key: str) -> Any:
    """Read a harness measurement, accepting the WS-1 ``host_<key>`` name or the bare ``<key>``."""
    value = host.get(f"host_{key}")
    return host.get(key) if value is None else value


def unit_diagnostics(
    cand_events: dict[str, Any],
    ref_events: dict[str, Any],
    *,
    cpus: float = CONTRACT_CPUS,
    gpu: bool = CONTRACT_GPU,
    host: dict[str, Any] | None = None,
) -> Diagnostics:
    """Diagnostics for one unit from the candidate + reference ``events.json`` dicts.

    When ``host`` (an independent harness measurement carrying ``wall_clock_sec`` and, if measured,
    ``n_events`` / ``peak_memory_bytes`` / ``gpu_seconds``, as written by
    ``throughput.run_unit --host-metrics-out``) is supplied, those values are used and the result is
    marked host-measured. Otherwise the submission's own numbers are used and the result is flagged
    ``telemetry_self_reported`` so it cannot win a telemetry-dependent award.
    """
    n_events = int(cand_events["n_events"])
    if host is not None:
        wall = float(_host_get(host, "wall_clock_sec") or 0.0)
        peak_mem = _host_get(host, "peak_memory_bytes")
        gpu_sec = _host_get(host, "gpu_seconds")
        # Prefer the harness's own event count (the emitted trace's row count). Using the declared
        # n_events over a host wall-clock would leave half the ranked fraction with the submission,
        # so an over-declared count would still inflate the rate.
        host_events = _host_get(host, "n_events")
        if host_events is not None:
            n_events = int(host_events)
        eps = (
            float(n_events) / wall if wall > 0 else float(cand_events["events_per_sec"])
        )
        # Provenance is per-measurement, not merely "a host dict was supplied". Every documented
        # degradation path (no NVML, unreadable cgroup, a harness that measured only some fields)
        # leaves the missing value None and falls back to the submission's own number, so the unit
        # must stay flagged. Since award eligibility keys on gpu_seconds > 0, an unmeasured GPU time
        # must not be allowed to look host-measured.
        self_reported = not (wall > 0 and peak_mem is not None and gpu_sec is not None)
    else:
        wall = float(cand_events.get("wall_clock_sec", 0.0))
        peak_mem = cand_events.get("peak_memory_bytes")
        gpu_sec = cand_events.get("gpu_seconds")
        eps = float(cand_events["events_per_sec"])
        self_reported = True
    return compute(
        events_per_sec=eps,
        n_events=n_events,
        wall_clock_sec=wall,
        peak_memory_bytes=peak_mem,
        gpu_seconds=gpu_sec,
        baseline_events_per_sec=float(ref_events.get("events_per_sec", 0.0)) or None,
        cpus=cpus,
        gpu=gpu,
        telemetry_self_reported=self_reported,
    )


def aggregate(per_unit: list[Diagnostics]) -> dict[str, Any]:
    """Median of each diagnostic across units (skipping ``None`` values)."""
    return {
        "median_speedup_vs_cpu_abides": _median(
            [
                d.speedup_vs_cpu_abides
                for d in per_unit
                if d.speedup_vs_cpu_abides is not None
            ]
        ),
        "median_efficiency": _median(
            [d.efficiency for d in per_unit if d.efficiency is not None]
        ),
        "median_memory_efficiency": _median(
            [d.memory_efficiency for d in per_unit if d.memory_efficiency is not None]
        ),
        "n_units": len(per_unit),
        # How many units' GPU-time / peak-memory telemetry was self-reported (no host measurement).
        # A non-zero count means the telemetry-dependent awards are advisory, not integrity-backed.
        "n_telemetry_self_reported": sum(
            1 for d in per_unit if d.telemetry_self_reported
        ),
    }


def _env(unit_dir: Path) -> tuple[float, bool]:
    """Read ``[environment]`` cpus/gpu from a unit card, else the frozen contract (4, true)."""
    card = unit_dir / "card.toml"
    if not card.exists():
        return CONTRACT_CPUS, CONTRACT_GPU
    env = tomllib.loads(card.read_text()).get("environment", {})
    return float(env.get("cpus", CONTRACT_CPUS)), bool(env.get("gpu", CONTRACT_GPU))


def build_report(
    submission_dir: Path,
    reference_dir: Path,
    *,
    host_metrics: dict[str, dict[str, Any]] | None = None,
    env_for: Callable[[str], tuple[float, bool]] | None = None,
) -> dict[str, Any]:
    """Walk matching ``<unit>/events.json`` under both dirs and build the diagnostics report.

    ``host_metrics`` maps a unit name to its independent harness measurement
    (``{host_wall_clock_sec, host_n_events, host_peak_memory_bytes, host_gpu_seconds}``, as written
    by ``throughput.run_unit --host-metrics-out``); a unit present there is integrity-backed, and
    one absent falls back to the submission's self-report and is flagged. ``env_for`` overrides the
    per-unit ``(cpus, gpu)`` lookup for callers whose reference tree does not put the card where
    ``_env`` looks for it.
    """
    per_unit: list[Diagnostics] = []
    units: dict[str, Any] = {}
    for sub in sorted(p for p in submission_dir.iterdir() if p.is_dir()):
        cand_ev = sub / "events.json"
        ref_ev = reference_dir / sub.name / "events.json"
        if not (cand_ev.exists() and ref_ev.exists()):
            continue
        cpus, gpu = (
            env_for(sub.name) if env_for is not None else _env(reference_dir / sub.name)
        )
        d = unit_diagnostics(
            json.loads(cand_ev.read_text()),
            json.loads(ref_ev.read_text()),
            cpus=cpus,
            gpu=gpu,
            host=host_metrics.get(sub.name) if host_metrics else None,
        )
        per_unit.append(d)
        units[sub.name] = d.to_dict()
    return {"units": units, "aggregate": aggregate(per_unit)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="throughput.report")
    ap.add_argument("--submission", required=True, type=Path)
    ap.add_argument("--reference", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args(argv)
    report = build_report(args.submission, args.reference)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["aggregate"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
