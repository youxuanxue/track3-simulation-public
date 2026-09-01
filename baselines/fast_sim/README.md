# fast_sim — Track 3 participant simulator

## Executive summary

`fast_sim` is a **bit-faithful accelerator of the pinned ABIDES baseline**, not a
new matching engine. The kernel, limit-order book, agents, oracle and latency
model stay the pinned ABIDES objects. Speed comes from work that does **not**
appear in the scored traces, plus a compiled (Cython) copy of the OrderBook
and Kernel hot path that is required to stay bit-exact with those traces.

Phase 1: unlocked `heapq`, filtered agent logs, skipped book-log snapshots,
parallel `simulate-batch`.

Phase 2: Cython `OrderBook` + `Kernel` hot path. Partial fills update remaining
quantity in place after a **cheap field-copy snapshot** that keeps `order_id`
(no counter bump). The accept path still stores a **second** order object in
the book — ABIDES `deepcopy` here is snapshotting, not optional work. GPU is
unused. CUDA / `sm_100` is not compiled; the default path is CPU.

## Why Cython (not Rust / C++)

Agents still `isinstance` Python `Message` / `LimitOrder` objects, and
`message_id` is assigned in `Message.__post_init__` in construction order.
A Rust or C++ book that owned its own order structs would have to cross FFI
on every `send_message` and would re-implement identity that the ledger and
the agents both read. Cython replaces `execute_order` / `handle_limit_order` /
`Kernel.runner` / `Kernel.send_message` while sharing those objects.

If a compiled book cannot match ABIDES fills, we stop rather than ship a
near-miss. The Python twin `fast_sim.hotpath` is the same algorithm and is
the import fallback when the extension is missing.

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
# compile the Cython extension (optional; Python fallback is bit-exact)
cd baselines && python setup_fast_sim.py build_ext --inplace

PYTHONPATH=baselines python -m fast_sim.simulate \
    --config regression_suite/scenarios/s001_price_time_priority.json \
    --out /tmp/fast_s001/trace.parquet
```

Requires the pinned ABIDES commit with the four `baselines/patches/` overlays
installed (same as `baselines/Dockerfile`). The participant `Dockerfile`
compiles the extension at image-build time (`linux/amd64`, vendored, no
runtime network).

## What is preserved

- ABIDES event order (`deliver_at`, then `message_id`)
- fill sequence, prices, sizes, `order_id`s
- message-ledger causality (`t_recv - t_send == latency_ns`)
- RNG / agent schedule order (`Kernel(random_state=RandomState(seed=0))`)
- STP `cancel_newest` / `cancel_oldest`
- partial-fill time priority (snapshot the resting order, then decrement qty)

## Local semantic + throughput results (this host, not the B200 fleet)

Official ranking is host-measured parquet-row-count / wall clock. Machines
differ; numbers below are directional. Traces were compared to the shipped
unit references (exact fill sequence, exact event coverage, exact timestamps,
exact message ledger).

**Phase 2: 65/65 public single-scenario units exact. 6/6 public batch units
exact (30/30 isolated subs).** Official `run_regression.py` was not run here
(no Docker daemon in this environment).

| Family | Public units | Result |
|---|---|---|
| 1 matching-engine-semantics | 14 | 14/14 exact |
| 2 / AS agent-mix + ST | 10 | 10/10 exact |
| 3 latency-profile (EQ + s019) | 9 | 9/9 exact |
| 4 / CA + SF calibration | 12 | 12/12 exact |
| 6 throughput-scale (`gb_*`) | 6 | 6/6 exact |
| 7 exchange-protocol (MP) | 7 | 7/7 exact |
| 8 reactive-agent (RA) | 6 | 6/6 exact |
| GB `t3-gbatch-*` | 6 batch (30 subs) | 30/30 exact |

| Scenario | Shipped ev/s | Phase 1 | Phase 2 | vs Phase 1 |
|---|---|---|---|---|
| `s001_price_time_priority` | 3,471 | 17,062 | 20,751 | 1.22× (tiny; startup-heavy) |
| `as06_throughput_fast` | 14,183 | 19,746 | **36,500** | **1.85×** |
| `gb_mega_throughput` | 10,571 | 22,421 | **28,129** | **1.25×** |
| `t3-gbatch-homog-8` (aggregate) | — | 61,779 | 69,577 | 1.13× |

`events_per_sec` is `n_events / wall_clock_sec` of the simulation loop
(same convention as the adapter). Consistency is exact, well inside ±5%.
Phase 1 and Phase 2 were measured on the **same host**; the official B200
worker will differ.
