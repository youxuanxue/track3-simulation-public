# fast_sim — Track 3 participant simulator

## Executive summary

`fast_sim` is a **bit-faithful accelerator of the pinned ABIDES baseline**, not a
new matching engine. The kernel, limit-order book, agents, oracle and latency
model stay the pinned ABIDES objects. Speed comes from work that does **not**
appear in the scored traces, a compiled (Cython) OrderBook / Kernel hot path,
and the Phase 3 / Phase 4 cuts of the Python tax each profiler pass named.

GPU is unused. CUDA / `sm_100` is not compiled; the default path is CPU.

## Why Cython (not Rust / C++)

Agents still `isinstance` Python `Message` / `LimitOrder` objects, and
`message_id` is assigned in `Message.__post_init__` in construction order.
Cython replaces `execute_order` / `handle_limit_order` / `Kernel.runner` /
`Kernel.send_message` while sharing those objects.

`deepcopy` on a partial fill is snapshotting: the book copy is mutated in
place after the fill snapshot; the accept message keeps the working residual.

## What Phase 4 actually was hot (after Phase 3)

Unprofiled split of the Phase 3 binary on this host:

| Unit | kernel | extract_trace | extract_msg | kernel-only ev/s | full ev/s |
|---|---|---|---|---|---|
| `as06_throughput_fast` | 0.90s (78%) | 0.13s | 0.11s | 82.7k | 64.9k |
| `gb_mega_throughput` | 19.0s (72%) | 2.72s (10%) | **4.46s (17%)** | 61.5k | 44.6k |

cProfile (gb_mega, Phase 3): `ExchangeAgent.receive_message` 3.9 tot / 18.9 cum
(`isinstance` + ABC + `logEvent` on every QuerySpread); `Agent.send_message`
4.2 tot (Cython send + heap); `PriceLevel.__init__` 2.5s / 67k calls;
`HeapPQueue.put` 1.4 tot vs `heappush` 0.28; extract join+sort of ~1.4M ledger
dicts.

Phase 4: `type()` dispatch on Exchange / Trading / Scheduled agents (fall
through for MarketHours / post-close); `ExchangeAgent.send_message` still
applies `pipeline_delay` to ack/fill/cancel; inline `heappush`/`heappop` on
the same `(deliver_at, (sender_id, recipient_id, message))` tuples; stamp
`seq` onto a delivered list at final delivery so extract skips the join+sort;
`PriceLevel.__init__` one-order fast path.

Phase 4 split of the new binary:

| Unit | kernel | extract_trace | extract_msg | kernel-only ev/s | full ev/s |
|---|---|---|---|---|---|
| `as06_throughput_fast` | 0.68s (80%) | 0.13s | 0.04s | 109k | 86.9k |
| `gb_mega_throughput` | 14.6s (81%) | 2.67s (15%) | **0.80s (4%)** | 80.2k | 64.8k |

New hot spots: Exchange send/recv wrappers (Cython `send_message` time lands
on the Python caller), `LimitOrder.__init__` / `Message.__post_init__`,
NoiseTrader `randint` + `act`/`wakeup`, `extract_trace`. `PriceLevel.__init__`,
`HeapPQueue.put`, and the ledger join+sort left the top 25.

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

**Phase 4: Family 1 14/14 exact. as06, gb_mega, MP (`mp01`, `mp02`) and RA
(`ra01`, `ra02`) exact.** Phase 2 had already shown 65/65 + 30/30; this
revision re-ran the mandatory set with no regressions. Official
`run_regression.py` was not run (no Docker daemon here).

| Scenario | Shipped ev/s | Phase 1 | Phase 2 | Phase 3 | Phase 4 | vs Phase 3 |
|---|---|---|---|---|---|---|
| `as06_throughput_fast` | 14,183 | 19,746 | 36,500 | 66,030 | **84,202** | **1.27×** |
| `gb_mega_throughput` | 10,571 | 22,421 | 28,129 | 48,259 | **66,921** | **1.39×** |

Repeats on the same host: as06 73.9k–84.2k, gb_mega 46.3k–66.9k. Best-of-N
is the same method Phase 3 used (as06 56.7k–66.0k, gb_mega 43.2k–48.3k).
Every as06 repeat is above the Phase 3 best; gb_mega's best and median
repeats are above Phase 3, with one thermally-slower repeat overlapping
the old range. The official B200 worker will differ.

`events_per_sec` is `n_events / wall_clock_sec` of the simulation loop.
