"""``simulate`` CLI — Track 3 submission verb for the fast_sim participant image.

Usage (Docker harness contract)::

    simulate --config /input/scenario.json --out /output/trace.parquet
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import resource
import time
from typing import Any, Optional

from abides_fork.scenario_io import read_scenario
from fast_sim.engine import run_scenario


def _sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _peak_rss_bytes() -> int:
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024


def _write_parquet(df, path: pathlib.Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, compression="snappy", index=False)


def simulate(
    config_path: str | pathlib.Path,
    out_path: str | pathlib.Path,
    seed: Optional[int] = None,
) -> dict[str, Any]:
    """Run the scenario, write ``trace.parquet`` + sidecars, return metadata."""
    scenario = json.loads(read_scenario(config_path))
    if seed is not None:
        scenario = {**scenario, "seed": int(seed)}

    t0 = time.perf_counter()
    trace, message_trace, _end_state = run_scenario(scenario)
    wall_clock_sec = time.perf_counter() - t0
    peak_memory_bytes = _peak_rss_bytes()

    out_path = pathlib.Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _write_parquet(trace, out_path)
    msg_out = out_path.parent / "message_trace.parquet"
    _write_parquet(message_trace, msg_out)

    n_events = int(len(trace))
    events = {
        "scenario_id": str(scenario["scenario_id"]),
        "seed": int(scenario["seed"]),
        "n_events": n_events,
        "wall_clock_sec": float(wall_clock_sec),
        "events_per_sec": float(n_events / wall_clock_sec) if wall_clock_sec > 0 else 0.0,
        "trace_sha256": _sha256(out_path),
        "n_messages": int(len(message_trace)),
        "message_trace_sha256": _sha256(msg_out),
        "peak_memory_bytes": peak_memory_bytes,
        "gpu_seconds": 0.0,
    }
    (out_path.parent / "events.json").write_text(json.dumps(events, indent=2) + "\n")
    return events


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="fast_sim.simulate")
    ap.add_argument("verb", nargs="?", default="simulate", choices=["simulate"])
    ap.add_argument("--config", required=True, help="path to scenario.json")
    ap.add_argument("--out", required=True, help="output path for trace.parquet")
    ap.add_argument("--seed", type=int, default=None, help="override scenario seed")
    args = ap.parse_args(argv)
    events = simulate(args.config, args.out, args.seed)
    print(json.dumps(events))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
