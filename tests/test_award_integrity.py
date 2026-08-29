"""Award-integrity tests for the telemetry-dependent special awards.

Proves the fix for the audit finding: the two telemetry-dependent awards (Best GPU Acceleration,
Best Systems Diagnosis) cannot be won from self-reported telemetry — a submission that fabricates a
tiny ``gpu_seconds`` or a self-consistent SimProfile does not win, and the awards yield no winner
until an independent host measurement is present.

Run in CI by the `firewall` job (stdlib + pyarrow, no secret), and locally as proof:

    python tests/test_award_integrity.py

# Fixture ids are SYN-prefixed on purpose. They previously used the same two-letter-plus-digits shape as the
# PRIVATE sealed index labels -- not a leak (the naming is sequential and the count is already
# public, and participants only ever see opaque u- handles), but it made the automated
# public-diff sweep for sealed identifiers return false positives, and a sweep with known false
# positives is a sweep people stop reading. Track 2 hit the same thing and moved its fixture too.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from throughput.diagnostics import GPU_UTILIZATION_FLOOR  # noqa: E402
from throughput.awards import (  # noqa: E402
    AwardEntry,
    best_gpu_acceleration,
    best_systems_diagnosis,
)
from throughput.report import build_report, unit_diagnostics  # noqa: E402
from throughput.simprofile import verify_profile  # noqa: E402
from throughput.timer import (  # noqa: E402
    RunMetrics,
    _CgroupMemorySampler,
    _GpuSampler,
)


_GPU_UNIT = "events_per_gpu_hour"


def _gpu_entry(label: str, **over: object) -> AwardEntry:
    """An entry eligible for Best GPU Acceleration, so each test can spoil exactly one thing."""
    kw: dict = {
        "label": label,
        "efficiency": 1.0e9,
        "efficiency_unit": _GPU_UNIT,
        "speedup_vs_cpu_abides": 25.0,
        "gpu_utilization": 0.80,
        "is_gpu": True,
        "telemetry_self_reported": False,
    }
    kw.update(over)
    return AwardEntry(**kw)  # type: ignore[arg-type]


def test_gpu_award_excludes_self_reported() -> None:
    # Self-reported telemetry decides both eligibility and the ranked figure, so it is fabricable in
    # both directions and must never win.
    cheater = _gpu_entry("cheater", speedup_vs_cpu_abides=1e6, telemetry_self_reported=True)
    honest = _gpu_entry("honest", speedup_vs_cpu_abides=2.0)
    assert best_gpu_acceleration([cheater, honest]) == "honest"
    # With only self-reported entries, no winner (advisory, not fabricable).
    assert best_gpu_acceleration([cheater]) is None


def test_award_entry_defaults_are_fail_closed() -> None:
    # An assembler that does not thread the provenance flag must LOSE, not silently win: the
    # assembler lives outside this repo, so eligibility has to be asserted, never assumed.
    naive = AwardEntry(
        label="unknown_provenance",
        efficiency=1e12,
        is_gpu=True,
        systems_diagnosis_score=1.0,
        efficiency_unit=_GPU_UNIT,
    )
    assert best_gpu_acceleration([naive]) is None
    assert best_systems_diagnosis([naive]) is None


def test_gpu_award_excludes_a_run_whose_gpu_was_never_sampled() -> None:
    # A unit whose NVML sample was missing has no measured utilization, so it cannot be shown to
    # have used the device and is ineligible however good its other numbers look. Previously this
    # was caught by the efficiency_unit check; utilization now catches it at the source.
    unsampled = _gpu_entry(
        "unsampled",
        gpu_utilization=None,
        efficiency=9e5,
        efficiency_unit="events_per_cpu_core_hour",
        speedup_vs_cpu_abides=1e6,
    )
    real = _gpu_entry("real", speedup_vs_cpu_abides=12.0)
    assert best_gpu_acceleration([unsampled, real]) == "real"
    assert best_gpu_acceleration([unsampled]) is None


def test_gpu_award_is_not_won_by_touching_the_device_least() -> None:
    # The regression this award was rebuilt for. `efficiency = events_per_sec / gpu_hours` puts GPU
    # time in the DENOMINATOR, so ranking on it hands the award to whoever uses the device least: a
    # CPU simulator issuing one tiny memcpy measured ~3,700x the efficiency of a saturated genuine
    # port. Host-side measurement does not help — those gpu_seconds are honest.
    genuine = _gpu_entry("genuine", gpu_utilization=0.91, speedup_vs_cpu_abides=30.8,
                         efficiency=1.76e9)
    token = _gpu_entry("token-memcpy", gpu_utilization=0.0003, speedup_vs_cpu_abides=23.1,
                       efficiency=2.7e12)
    assert max([genuine, token], key=lambda e: e.efficiency).label == "token-memcpy"  # old basis
    assert best_gpu_acceleration([genuine, token]) == "genuine"  # corrected basis


def test_a_utilization_floor_alone_would_not_have_fixed_it() -> None:
    # Why the ranked quantity had to change and not just eligibility: with gpu_seconds still in the
    # denominator, a floor only relocates the optimum onto itself. This submission runs a trivial
    # kernel to just over the threshold, so it is ELIGIBLE, and on the old basis it still wins.
    # Ranking on speedup — which contains no gpu_seconds — is what actually removes the incentive.
    genuine = _gpu_entry("genuine", gpu_utilization=0.91, speedup_vs_cpu_abides=30.8,
                         efficiency=1.76e9)
    gamer = _gpu_entry("just-over-the-floor", gpu_utilization=0.051,
                       speedup_vs_cpu_abides=23.1, efficiency=1.76e10)
    assert gamer.gpu_utilization is not None
    assert gamer.gpu_utilization >= GPU_UTILIZATION_FLOOR  # clears eligibility
    assert max([genuine, gamer], key=lambda e: e.efficiency).label == "just-over-the-floor"
    assert best_gpu_acceleration([genuine, gamer]) == "genuine"


def test_utilization_is_eligibility_only_never_the_ranking() -> None:
    # Ranking on utilization would invert the defect the other way, paying submissions to keep the
    # device busy for its own sake. The lower-utilization submission wins here because it is faster.
    busy = _gpu_entry("busy-but-slower", gpu_utilization=0.95, speedup_vs_cpu_abides=11.0)
    lean = _gpu_entry("leaner-but-faster", gpu_utilization=0.30, speedup_vs_cpu_abides=26.0)
    assert best_gpu_acceleration([busy, lean]) == "leaner-but-faster"


def test_partial_host_metrics_stay_flagged() -> None:
    # A host mapping that is missing a measurement (no NVML on a GPU unit, invisible cgroup, or a
    # harness that left wall-clock to events.json) must NOT be treated as host-measured, because
    # each missing value silently falls back to the submission's own number.
    cand = {
        "events_per_sec": 1e9,
        "n_events": 1000,
        "wall_clock_sec": 1.0,
        "peak_memory_bytes": 1000,
        "gpu_seconds": 1e-4,
    }
    ref = {"events_per_sec": 100.0}
    no_gpu_sample = {"host_wall_clock_sec": 1.0, "host_peak_memory_bytes": 4096}
    assert (
        unit_diagnostics(
            cand, ref, cpus=4.0, gpu=True, host=no_gpu_sample
        ).telemetry_self_reported
        is True
    )
    no_wall = {"host_gpu_seconds": 2.0, "host_peak_memory_bytes": 4096}
    assert (
        unit_diagnostics(
            cand, ref, cpus=4.0, gpu=True, host=no_wall
        ).telemetry_self_reported
        is True
    )
    no_mem = {"host_wall_clock_sec": 1.0, "host_gpu_seconds": 2.0}
    assert (
        unit_diagnostics(
            cand, ref, cpus=4.0, gpu=True, host=no_mem
        ).telemetry_self_reported
        is True
    )
    # Every Track-3 card now declares gpu = true, so a missing GPU sample is never "expected":
    # without it we cannot tell whether the run touched the device, and eligibility for the GPU
    # award keys on exactly that. A unit missing it therefore stays flagged even on a CPU-shaped run.
    assert (
        unit_diagnostics(
            cand, ref, cpus=4.0, gpu=False, host=no_gpu_sample
        ).telemetry_self_reported
        is True
    )
    # All three measured: integrity-backed.
    full = {
        "host_wall_clock_sec": 1.0,
        "host_peak_memory_bytes": 4096,
        "host_gpu_seconds": 2.0,
    }
    assert (
        unit_diagnostics(
            cand, ref, cpus=4.0, gpu=True, host=full
        ).telemetry_self_reported
        is False
    )


def test_systems_diagnosis_excludes_self_reported() -> None:
    cheater = AwardEntry(
        label="cheater", systems_diagnosis_score=1.0, telemetry_self_reported=True
    )
    honest = AwardEntry(
        label="honest", systems_diagnosis_score=0.8, telemetry_self_reported=False
    )
    assert best_systems_diagnosis([cheater, honest]) == "honest"
    assert best_systems_diagnosis([cheater]) is None


def test_verify_profile_zeroes_on_self_reported_ground_truth() -> None:
    # A perfectly self-consistent profile (components sum to the wall-clock).
    profile = {
        "components": {
            "matching": 0.5,
            "event_queue": 0.3,
            "latency": 0.1,
            "agent_logic": 0.1,
        },
        "gpu_utilization": 0.0,
        "peak_memory_bytes": 1000,
    }
    # Against an independent host measurement it passes.
    v_host = verify_profile(profile, wall_clock_sec=1.0, peak_memory_bytes=1000)
    assert v_host.valid and v_host.quality > 0.0
    # Against self-reported ground truth, quality is forced to 0 (consistency != integrity).
    v_self = verify_profile(
        profile,
        wall_clock_sec=1.0,
        peak_memory_bytes=1000,
        ground_truth_self_reported=True,
    )
    assert (not v_self.valid) and v_self.quality == 0.0


def test_unit_diagnostics_prefers_host_and_flags_provenance() -> None:
    # Candidate fabricates gpu_seconds=1e-4 to inflate GPU efficiency.
    cand = {
        "events_per_sec": 1e9,
        "n_events": 1000,
        "wall_clock_sec": 1.0,
        "peak_memory_bytes": 1000,
        "gpu_seconds": 1e-4,
    }
    ref = {"events_per_sec": 100.0}

    d_self = unit_diagnostics(cand, ref, cpus=4.0, gpu=True)
    assert d_self.telemetry_self_reported is True

    # An independent host measurement (gpu_seconds=2.0) is used instead of the fabricated 1e-4,
    # and events_per_sec is recomputed from the gate-verified n_events over the host wall-clock.
    host = {"wall_clock_sec": 1.0, "peak_memory_bytes": 1000, "gpu_seconds": 2.0}
    d_host = unit_diagnostics(cand, ref, cpus=4.0, gpu=True, host=host)
    assert d_host.telemetry_self_reported is False
    assert d_host.efficiency is not None
    expected = (1000.0 / 1.0) / (2.0 / 3600.0)  # host events/sec over host GPU-hours
    assert abs(d_host.efficiency - expected) < 1.0
    # The self-reported efficiency would have been vastly larger; the host value neutralizes it.
    assert d_self.efficiency is not None and d_self.efficiency > d_host.efficiency * 1e6


def test_cpu_only_degrades_cleanly() -> None:
    cand = {
        "events_per_sec": 500.0,
        "n_events": 500,
        "wall_clock_sec": 1.0,
        "peak_memory_bytes": 1000,
        "gpu_seconds": 0.0,
    }
    d = unit_diagnostics(cand, {"events_per_sec": 100.0}, cpus=4.0, gpu=False)
    assert d.efficiency_unit == "events_per_cpu_core_hour"
    assert d.telemetry_self_reported is True
    assert d.memory_efficiency == 500.0 / 1000.0


def test_build_report_routes_host_metrics() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        sub_dir = root / "sub" / "SYN001"
        ref_dir = root / "ref" / "SYN001"
        sub_dir.mkdir(parents=True)
        ref_dir.mkdir(parents=True)
        (sub_dir / "events.json").write_text(
            json.dumps(
                {
                    "events_per_sec": 1e9,
                    "n_events": 1000,
                    "wall_clock_sec": 1.0,
                    "peak_memory_bytes": 1000,
                    "gpu_seconds": 1e-4,  # fabricated
                }
            )
        )
        (ref_dir / "events.json").write_text(json.dumps({"events_per_sec": 100.0}))

        # No host metrics -> flagged self-reported.
        rep = build_report(root / "sub", root / "ref", env_for=lambda name: (4.0, True))
        assert rep["aggregate"]["n_telemetry_self_reported"] == 1
        assert rep["units"]["SYN001"]["telemetry_self_reported"] is True

        # Host metrics present -> integrity-backed, and the fabricated gpu_seconds is ignored.
        rep_h = build_report(
            root / "sub",
            root / "ref",
            host_metrics={
                "SYN001": {
                    "wall_clock_sec": 1.0,
                    "peak_memory_bytes": 1000,
                    "gpu_seconds": 2.0,
                }
            },
            env_for=lambda name: (4.0, True),
        )
        assert rep_h["aggregate"]["n_telemetry_self_reported"] == 0
        assert rep_h["units"]["SYN001"]["telemetry_self_reported"] is False


def test_gpu_sampler_graceful_without_nvml() -> None:
    # On a box with no NVML/GPU the sampler must degrade to None, not raise.
    with _GpuSampler(interval_s=0.01) as s:
        time.sleep(0.05)
    assert s.result() is None


def test_cgroup_sampler_graceful_without_cgroup() -> None:
    # No cidfile / no readable cgroup (e.g. Docker Desktop on macOS) must degrade to None, not raise.
    with tempfile.TemporaryDirectory() as tmp:
        cidfile = Path(tmp) / "container.cid"
        with _CgroupMemorySampler(cidfile, interval_s=0.01) as s:
            time.sleep(0.05)
        assert s.result() is None


def test_cgroup_sampler_tracks_peak_from_a_cgroup_dir() -> None:
    # Simulate a cgroup directory: the sampler must find the container id from the cidfile and keep
    # the maximum observed value (here memory.current, as on a kernel without memory.peak).
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cgroup = root / "cgroup"
        cgroup.mkdir()
        (cgroup / "memory.current").write_text("1000\n")
        cidfile = root / "container.cid"
        cidfile.write_text("abc123\n")

        sampler = _CgroupMemorySampler(cidfile, interval_s=0.01)
        sampler._cgroup_dir = staticmethod(lambda cid: cgroup)  # type: ignore[method-assign]
        with sampler:
            time.sleep(0.05)
            (cgroup / "memory.current").write_text("5000\n")  # spike
            time.sleep(0.05)
            (cgroup / "memory.current").write_text("2000\n")  # falls back down
            time.sleep(0.05)
        assert sampler.result() == 5000  # the PEAK, not the last sample


def test_run_metrics_exposes_host_field_names() -> None:
    # WS-1 interface contract: the harness emits host_wall_clock_sec / host_gpu_seconds /
    # host_peak_memory_bytes, and report.build_report consumes exactly those keys.
    m = RunMetrics(
        events_per_sec=1000.0,
        host_wall_clock_sec=2.0,
        host_gpu_seconds=0.5,
        host_peak_memory_bytes=4096,
    )
    hm = m.as_host_metrics()
    assert set(hm) == {
        "host_wall_clock_sec",
        "host_gpu_seconds",
        "host_peak_memory_bytes",
    }
    cand = {
        "events_per_sec": 1e9,
        "n_events": 2000,
        "wall_clock_sec": 0.001,
        "peak_memory_bytes": 1,
        "gpu_seconds": 1e-4,
    }
    d = unit_diagnostics(cand, {"events_per_sec": 100.0}, cpus=4.0, gpu=True, host=hm)
    assert d.telemetry_self_reported is False
    # events/sec recomputed from gate-verified n_events over the HOST wall-clock, not the claim.
    assert d.speedup_vs_cpu_abides is not None
    assert abs(d.speedup_vs_cpu_abides - (2000.0 / 2.0) / 100.0) < 1e-9
    # memory efficiency uses the host peak, not the submission's "1 byte".
    assert d.memory_efficiency is not None
    assert abs(d.memory_efficiency - 2000.0 / 4096.0) < 1e-9


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
