# fast_sim — Track 3 participant simulator

## Executive summary

`fast_sim` is a **bit-faithful accelerator of the pinned ABIDES baseline**, not a
new matching engine. The kernel, limit-order book, agents, oracle and latency
model stay the pinned ABIDES objects. Speed comes from work that does **not**
appear in the scored traces, a compiled (Cython) OrderBook / Kernel hot path,
and the Phase 3–5 cuts of the Python tax each profiler pass named.

GPU is unused. CUDA / `sm_100` is not compiled; the default path is CPU.

## Why Cython (not Rust / C++)

Agents still `isinstance` Python `Message` / `LimitOrder` objects, and
`message_id` is assigned in `Message.__post_init__` in construction order.
Cython replaces `execute_order` / `handle_limit_order` / `Kernel.runner` /
`Kernel.send_message` while sharing those objects.

`deepcopy` on a partial fill is snapshotting: the book copy is mutated in
place after the fill snapshot; the accept message keeps the working residual.

## What Phase 5 actually was hot (after Phase 4)

Unprofiled split of the Phase 4 binary on this host:

| Unit | kernel | extract_trace | extract_msg | kernel-only ev/s | full ev/s |
|---|---|---|---|---|---|
| `as06_throughput_fast` | 0.68s (80%) | 0.13s | 0.04s | 109k | 86.9k |
| `gb_mega_throughput` | 14.6s (81%) | **2.67s (15%)** | 0.80s (4%) | 80.2k | 64.8k |

cProfile (gb_mega, Phase 4): Exchange send/recv wrappers (Cython
`send_message` time lands on the Python caller), `LimitOrder.__init__` /
`Message.__post_init__`, NoiseTrader `randint` + `act`/`wakeup`,
`extract_trace` walking dict logs.

Phase 5: fast `LimitOrder.__init__` (no Order ABC) and `__new__` messages
with the same `message_id` order; book path calls `kernel.send_message`
with `pipeline_delay`; thin `ScheduledAgent.wakeup` / `NoiseTrader.act` /
`place_limit_order` (same RNG draws); compact 7-tuple order / 4-tuple quote
logs; Cython `set_wakeup`; BEST_BID/ASK read cached `_visible_qty`.

Phase 5 split of the new binary:

| Unit | kernel | extract_trace | extract_msg | kernel-only ev/s | full ev/s |
|---|---|---|---|---|---|
| `as06_throughput_fast` | 0.54s (82%) | 0.08s | 0.04s | 138k | 113k |
| `gb_mega_throughput` | 12.5s (85%) | 1.44s (10%) | 0.82s (6%) | 93.7k | 79.3k |

New hot spots: `ExchangeAgent.receive_message` (still the QuerySpread /
handle_limit hub), `ScheduledAgent.wakeup`, `Kernel.run` (Cython loop
billed to Python), `place_limit_order` / `noise_act` / numpy `randint`.
`Message.__post_init__` and dict-log extract left the top 20.

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

**Phase 5: Family 1 14/14 exact. as06, gb_mega, MP (`mp01`, `mp02`) and RA
(`ra01`, `ra02`) exact.** Phase 2 had already shown 65/65 + 30/30; this
revision re-ran the mandatory set with no regressions. Official
`run_regression.py` was not run (no Docker daemon here).

| Scenario | Shipped | P1 | P2 | P3 | P4 | Phase 5 | vs Phase 4 |
|---|---|---|---|---|---|---|---|
| `as06_throughput_fast` | 14,183 | 19,746 | 36,500 | 66,030 | 84,202 | **119,608** | **1.42×** |
| `gb_mega_throughput` | 10,571 | 22,421 | 28,129 | 48,259 | 66,921 | **78,140** | **1.17×** |

Repeats on the same host: as06 106k–120k, gb_mega 56.3k–78.1k. Best-of-N
matches earlier phases. Every as06 repeat is above the Phase 4 best;
gb_mega's best is above Phase 4, with slower repeats overlapping the old
range (same thermal pattern as Phase 4). The official B200 worker will
differ.

`events_per_sec` is `n_events / wall_clock_sec` of the simulation loop.
