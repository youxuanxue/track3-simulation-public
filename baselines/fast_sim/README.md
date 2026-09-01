# fast_sim — Track 3 participant simulator

## Executive summary

`fast_sim` is a **bit-faithful accelerator of the pinned ABIDES baseline**, not a
new matching engine. The kernel, limit-order book, agents, oracle and latency
model stay the pinned ABIDES objects. Speed comes from work that does **not**
appear in the scored traces, a compiled (Cython) OrderBook / Kernel hot path,
and Phase 3 cuts of the Python tax the profiler named after Phase 2.

GPU is unused. CUDA / `sm_100` is not compiled; the default path is CPU.

## Why Cython (not Rust / C++)

Agents still `isinstance` Python `Message` / `LimitOrder` objects, and
`message_id` is assigned in `Message.__post_init__` in construction order.
Cython replaces `execute_order` / `handle_limit_order` / `Kernel.runner` /
`Kernel.send_message` while sharing those objects.

`deepcopy` on a partial fill is snapshotting: the book copy is mutated in
place after the fill snapshot; the accept message keeps the working residual.

## What Phase 3 actually was hot (after Phase 2)

Unprofiled split of the Phase 2 binary on this host:

| Unit | kernel | extract_trace | extract_msg |
|---|---|---|---|
| `as06_throughput_fast` | 1.71s | 0.22s | **0.42s (18%)** |
| `gb_mega_throughput` | 32.3s | 3.34s | **9.67s (21%)** |
| `ra01_fundamental_shock_mid` | 0.61s | 0.09s | 0.14s |

cProfile inside the kernel (gb_mega): `fmt_ts` / `LimitOrder.__str__` /
`str.format` / `dollarize` still ran because `logger.debug(f"... {order}")`
evaluates the f-string even when DEBUG is disabled; `PriceLevel.total_quantity`
re-summed the level on every BEST_BID/ASK and QuerySpread; `create_limit_order`
copied `holdings` even when `ignore_risk=True`.

Phase 3: cheap order `__str__`, cached visible qty (updated on the in-place
partial-fill decrement), numpy ledger/trace extract, skip the holdings copy
when `ignore_risk`.

## CLI

```
simulate       --config /input/scenario.json --out /output/trace.parquet
simulate-batch --batch-dir /input/scenarios   --out-dir /output
```

## Local run (no Docker)

```bash
cd baselines && python setup_fast_sim.py build_ext --inplace

PYTHONPATH=baselines python -m fast_sim.simulate \
    --config regression_suite/scenarios/s001_price_time_priority.json \
    --out /tmp/fast_s001/trace.parquet
```

Requires the pinned ABIDES commit with the four `baselines/patches/` overlays.
The participant `Dockerfile` compiles the extension at image-build time
(`linux/amd64`, vendored, no runtime network).

## Local semantic + throughput results (this host, not the B200 fleet)

Official ranking is host-measured parquet-row-count / wall clock. Machines
differ. Traces were compared to the shipped unit references (exact fills,
coverage, timestamps, message ledger).

**Phase 3: Family 1 14/14 exact. as06, gb_mega, MP (`mp01`, `mp02`) and RA
(`ra01`, `ra02`) exact.** Phase 2 had already shown 65/65 + 30/30; this
revision re-ran the mandatory set with no regressions. Official
`run_regression.py` was not run (no Docker daemon here).

| Scenario | Shipped ev/s | Phase 1 | Phase 2 | Phase 3 | vs Phase 2 |
|---|---|---|---|---|---|
| `as06_throughput_fast` | 14,183 | 19,746 | 36,500 | **66,030** | **1.81×** |
| `gb_mega_throughput` | 10,571 | 22,421 | 28,129 | **48,259** | **1.72×** |

Repeats on the same host: as06 56.7k–66.0k, gb_mega 43.2k–48.3k. Every
repeat is above the Phase 2 numbers. The official B200 worker will differ.

`events_per_sec` is `n_events / wall_clock_sec` of the simulation loop.
