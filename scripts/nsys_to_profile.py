"""Convert an Nsight Systems (nsys) NVTX summary export into a SimProfile artifact that
``throughput.simprofile.verify_profile`` accepts.

Workflow (see docs/PROFILING.md): annotate your simulator's hot regions with NVTX ranges named to
match the SimProfile components (``matching`` / ``event_queue`` / ``latency`` / ``agent_logic`` /
``io``), profile a run on YOUR machine, export the NVTX summary as JSON, and run this converter. The
result is ``profile.json`` — the optional systems-diagnosis sidecar that feeds the Best Systems
Diagnosis award. Profiling happens on the participant's own machine; the profiler never touches the
ranked run.

Usage:
    nsys stats --report nvtx_sum --format json run.nsys-rep > nvtx.json
    python scripts/nsys_to_profile.py --nsys-json nvtx.json \\
        --gpu-util 0.72 --peak-memory 2147483648 --out profile.json \\
        [--wall-clock 3.4]     # optional: self-check against verify_profile
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Keys nsys uses across versions for the range name and its aggregated duration.
_NAME_KEYS = ("Range", "Name", "name", "range")
_DUR_KEYS = (
    "Total Time (ns)",
    "Total Time",
    "Total Time(ns)",
    "Duration (ns)",
    "duration_ns",
    "total_ns",
)
# Canonical SimProfile components; NVTX ranges should be named to one of these.
_COMPONENTS = ("matching", "event_queue", "latency", "agent_logic", "io")


def _first_key(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in row:
            return row[k]
    return None


def _rows(data: Any) -> list[dict[str, Any]]:
    """Normalize an nsys `--format json` export to a flat list of range rows. nsys emits either a
    flat list of row dicts, or a list of report objects each carrying a ``rows`` list."""
    if isinstance(data, dict):
        data = [data]
    rows: list[dict[str, Any]] = []
    for item in data:
        if isinstance(item, dict) and isinstance(item.get("rows"), list):
            rows.extend(r for r in item["rows"] if isinstance(r, dict))
        elif isinstance(item, dict):
            rows.append(item)
    return rows


def parse_nvtx_export(data: Any) -> dict[str, float]:
    """Return ``{range_name: total_seconds}`` from a parsed nsys NVTX summary export."""
    out: dict[str, float] = {}
    for row in _rows(data):
        name = _first_key(row, _NAME_KEYS)
        dur = _first_key(row, _DUR_KEYS)
        if name is None or dur is None:
            continue
        # NVTX names may be domain-qualified ("PushPop:matching", ":matching") — take the leaf.
        leaf = str(name).split(":")[-1].strip()
        try:
            seconds = float(dur) / 1e9
        except (TypeError, ValueError):
            continue
        out[leaf] = out.get(leaf, 0.0) + seconds
    return out


def to_profile(
    ranges: dict[str, float],
    *,
    gpu_utilization: float = 0.0,
    peak_memory_bytes: int = 0,
    keep_unknown: bool = True,
) -> dict[str, Any]:
    """Build a SimProfile dict from per-range seconds. Ranges matching a canonical component name are
    kept as-is; unrecognized ranges are kept under their own name when ``keep_unknown`` (so the
    profile still accounts for all measured time and the components sum to the wall-clock)."""
    components: dict[str, float] = {}
    for name, secs in ranges.items():
        if name in _COMPONENTS or keep_unknown:
            components[name] = round(components.get(name, 0.0) + secs, 9)
    return {
        "components": components,
        "gpu_utilization": float(gpu_utilization),
        "peak_memory_bytes": int(peak_memory_bytes),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="nsys_to_profile")
    ap.add_argument(
        "--nsys-json", required=True, type=Path, help="nsys nvtx_sum --format json"
    )
    ap.add_argument(
        "--gpu-util", type=float, default=0.0, help="mean GPU utilization in [0,1]"
    )
    ap.add_argument(
        "--peak-memory", type=int, default=0, help="peak resident memory (bytes)"
    )
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument(
        "--wall-clock",
        type=float,
        default=None,
        help="optional: host wall-clock (sec) to self-check via verify_profile",
    )
    args = ap.parse_args(argv)

    ranges = parse_nvtx_export(json.loads(args.nsys_json.read_text()))
    if not ranges:
        print("error: no NVTX ranges found in the export", file=sys.stderr)
        return 2
    profile = to_profile(
        ranges, gpu_utilization=args.gpu_util, peak_memory_bytes=args.peak_memory
    )
    args.out.write_text(json.dumps(profile, indent=2) + "\n")
    print(
        f"wrote {args.out} — {len(profile['components'])} components, "
        f"{sum(profile['components'].values()):.4f}s total"
    )

    if args.wall_clock is not None:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from throughput.simprofile import verify_profile

        v = verify_profile(
            profile, wall_clock_sec=args.wall_clock, peak_memory_bytes=args.peak_memory
        )
        print(
            f"self-check vs verify_profile: valid={v.valid} quality={v.quality:.3f} "
            f"breaches={v.breaches}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
