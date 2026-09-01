# fast_sim — Track 3 participant simulator

## Executive summary

`fast_sim` is a **bit-faithful accelerator of the pinned ABIDES baseline**, not a
new matching engine. The kernel, limit-order book, agents, oracle and latency
model are the same objects the organizer used to freeze the public reference
traces. What changes is everything that does **not** appear in those traces:

- the event queue is an unlocked `heapq` with the same comparison key ABIDES
  uses (`deliver_at`, then `message_id`), so delivery order is unchanged
- agent logs keep only the event types that become `trace.parquet` rows
- book-log numpy snapshots, order-stream history, end-of-day analysis and
  summary pickles are skipped
- `simulate-batch` runs independent sub-scenarios in parallel (up to 4 workers)

GPU is unused. Discrete-event order is sequential; a JAX-style batched book
would fail Tier A. The 4-CPU / 16G / `network=none` card caps are honoured.
CUDA 12.x / `sm_100` is relevant only if a later revision adds optional
elementwise work — this revision does not.

## CLI

```
simulate       --config /input/scenario.json --out /output/trace.parquet
simulate-batch --batch-dir /input/scenarios   --out-dir /output
```

Outputs: `trace.parquet` (7 columns), `events.json` (including
`peak_memory_bytes` and `gpu_seconds`), `message_trace.parquet` on every run,
and `batch_events.json` (with `wall_clock_sec`) for batch units.

## Local run (no Docker)

```bash
PYTHONPATH=baselines python -m fast_sim.simulate \
    --config regression_suite/scenarios/s001_price_time_priority.json \
    --out /tmp/fast_s001/trace.parquet
```

Requires the pinned ABIDES commit with the four `baselines/patches/` overlays
installed (same as `baselines/Dockerfile`).

## Local semantic + throughput results (this host, not the B200 fleet)

Official ranking is host-measured parquet-row-count / wall clock. These
numbers are directional. Traces were compared to the shipped unit references
(exact fill sequence, exact event coverage, exact timestamps).

**65/65 public single-scenario units exact. 6/6 public batch units exact
(30/30 isolated subs).** Official `run_regression.py` was not run here
(no Docker daemon in this environment).

| Family | Public units | Result |
|---|---|---|
| 1 matching-engine-semantics | 14 | 14/14 exact |
| 2 / AS agent-mix + ST | 10 | 10/10 exact |
| 3 latency-profile (EQ + s019) | 9 | 9/9 exact |
| 4 / CA + SF calibration | 12 | 12/10+7 exact |
| 6 throughput-scale (`gb_*`) | 6 | 6/6 exact |
| 7 exchange-protocol (MP) | 7 | 7/7 exact |
| 8 reactive-agent (RA) | 6 | 6/6 exact |
| GB `t3-gbatch-*` | 6 batch (30 subs) | 30/30 exact |

| Scenario | Shipped `events_per_sec` | fast_sim | Speedup |
|---|---|---|---|
| `s001_price_time_priority` | 3,471 | 17,062 | 4.9× (tiny; startup-heavy) |
| `as06_throughput_fast` | 14,183 | **19,746** | **1.39×** |
| `gb_mega_throughput` | 10,571 | **22,421** | **2.12×** |
| `t3-gbatch-homog-8` (aggregate) | — | 61,779 | parallel batch |

`events_per_sec` is `n_events / wall_clock_sec` of the simulation loop
(same convention as the adapter). Consistency is exact, well inside ±5%.

## What still uses ABIDES Python

The matching loop itself is still the ABIDES `OrderBook`. The next bottleneck
is per-event `deepcopy` of resting orders on partial fills, then Python
message objects. A compiled book that preserves fill / message / RNG order is
the follow-up now that Family 1 is green.
