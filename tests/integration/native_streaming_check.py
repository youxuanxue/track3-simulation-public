"""Compare compiled streaming output with the complete in-memory native engine.

Run inside the rebuilt participant image with this repository at /workspace:
python /workspace/tests/integration/native_streaming_check.py
These bounded differential cases are diagnostics, not the full G2 experiment.
"""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import pandas as pd
import pyarrow.parquet as pq

from abides_fork.config import build_config
from fast_sim.engine import reset_abides_counters
from fast_sim.native import as_pandas, snapshot_native, stream_native
from fast_sim._native import (
    classify_native_executions,
    run_native_sim,
    stream_native_sim,
)


ROOT = Path(__file__).resolve().parents[2]


def spec_for(scenario):
    reset_abides_counters()
    return snapshot_native(build_config(deepcopy(scenario)))


class NativeStreamingTests(unittest.TestCase):
    def compare(self, scenario, chunk_rows):
        spec = spec_for(scenario)
        started = time.perf_counter()
        original = run_native_sim(deepcopy(spec))
        buffered_seconds = time.perf_counter() - started
        with tempfile.TemporaryDirectory() as directory:
            paths = [
                Path(directory) / name
                for name in ("trace.parquet", "message_trace.parquet")
            ]
            started = time.perf_counter()
            results = stream_native(deepcopy(spec), paths, chunk_rows=chunk_rows)
            if original[0].num_rows > 1_000_000:
                print(
                    json.dumps(
                        {
                            "rankable": False,
                            "diagnostic": True,
                            "horizon_ns": scenario["horizon_ns"],
                            "buffered_kernel_and_conversion_sec": buffered_seconds,
                            "streaming_two_pass_and_write_sec": time.perf_counter()
                            - started,
                            "rows": [t.num_rows for t in results],
                            "files": {p.name: p.stat().st_size for p in paths},
                        }
                    ),
                    flush=True,
                )
            hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
            for expected, result, path in zip(original, results, paths):
                actual = pq.read_table(path)
                self.assertTrue(
                    actual.equals(expected, check_metadata=False), str(path)
                )
                self.assertEqual(result.num_rows, expected.num_rows)
                if "t_send_ns" in expected.column_names:
                    pd.testing.assert_frame_equal(
                        pd.read_parquet(path), as_pandas(expected)
                    )
                if expected.num_rows > chunk_rows * 5:
                    self.assertGreater(pq.ParquetFile(path).num_row_groups, 1)
            stream_native(deepcopy(spec), paths, chunk_rows=chunk_rows)
            self.assertEqual(
                hashes,
                [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths],
            )

    def test_public_families_and_long_latency(self):
        cases = [
            "t3-s001-price-time-priority",
            "t3-mp01-stp-newest-baseline",
            "t3-st05-latency-spike-pareto",
            "t3-ra01-fundamental-shock-mid",
            "t3-sf-05-deep-book-liquidity",
            "t3-ca-thin-book-depth",
            "t3-as06-throughput-fast",
        ]
        for unit in cases:
            with self.subTest(unit=unit):
                scenario = json.loads(
                    (ROOT / "units" / unit / "scenario.json").read_text()
                )
                # Bounded test only. The acceptance roster is never shortened.
                scenario["horizon_ns"] = min(scenario["horizon_ns"], 100_000_000)
                self.compare(scenario, 137)

    def test_reordered_execution_and_cancel_delivery(self):
        scenario = json.loads(
            (ROOT / "units/t3-mp01-stp-newest-baseline/scenario.json").read_text()
        )
        scenario["horizon_ns"] = 30_000_000
        scenario["latency_config"] = {
            "model": "uniform",
            "params": {"min_ns": 0, "max_ns": 7_000_000},
        }
        for policy in ("cancel_newest", "cancel_oldest"):
            scenario["exchange_config"]["stp_policy"] = policy
            for seed in (91571, 91572, 91573):
                with self.subTest(policy=policy, seed=seed):
                    scenario["seed"] = seed
                    self.compare(scenario, 3)

    def test_dense_exemplar_prefix(self):
        scenario = json.loads(
            (ROOT / "units/t3-EXAMPLE-vectorized-matching/scenario.json").read_text()
        )
        scenario["horizon_ns"] = 1_000_000_000
        self.compare(scenario, 262144)

    def test_replay_count_mismatch_is_an_error(self):
        scenario = json.loads(
            (ROOT / "units/t3-s001-price-time-priority/scenario.json").read_text()
        )
        scenario["horizon_ns"] = 10_000_000
        spec = spec_for(scenario)
        partial, count = classify_native_executions(deepcopy(spec))

        class Sink:
            def write(self, table):
                pass

        with self.assertRaisesRegex(ValueError, "classification pass"):
            stream_native_sim(deepcopy(spec), partial, count + 1, Sink(), Sink())

    def test_resource_failure_does_not_start_hybrid(self):
        from fast_sim.engine import run_scenario

        scenario = json.loads(
            (ROOT / "units/t3-s001-price-time-priority/scenario.json").read_text()
        )
        for error in (OSError("disk full"), MemoryError("allocation failed")):
            with self.subTest(error=type(error).__name__):
                with patch("fast_sim.native.run_native", side_effect=error), patch(
                    "fast_sim.engine.Kernel"
                ) as hybrid:
                    with self.assertRaises(type(error)):
                        run_scenario(scenario)
                    hybrid.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
