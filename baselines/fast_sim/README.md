# fast_sim — Track 3 participant simulator

## Executive summary

`fast_sim` is a **bit-faithful accelerator of the pinned ABIDES baseline**, not a
new matching engine. The kernel, limit-order book, agents, oracle and latency
model stay the pinned ABIDES objects. Speed comes from work that does **not**
appear in the scored traces, a compiled (Cython) OrderBook / Kernel hot path,
and the Phase 3–6 cuts of the Python tax each profiler pass named.
Phase 6 compiled the remaining hubs and then hit diminishing returns
(<10% on both throughput units). Championship step 1 replaced the
Python tuple heap with a C `EventQueue`. Step 2 inlines PriceLevel
ops and compiles `cancel_order` inside the same Cython book;
leftover agents stay Python. No GPU.

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

**Phase 6: Family 1 14/14 exact. as06, gb_mega, MP (`mp01`) and RA (`ra01`)
exact.** Phase 6 compiled Exchange receive / wakeup / place / `noise_act`
and stored the ledger as tuples. That was a real attempt at the Phase 5
hubs. **Both units moved <10% vs Phase 5** (as06 **1.08×**, gb_mega
inside the Phase 5 repeat band). Stop. No more micro-opts.

| Scenario | Shipped | P3 | P4 | P5 | Phase 6 | vs Phase 5 |
|---|---|---|---|---|---|---|
| `as06_throughput_fast` | 14,183 | 66,030 | 84,202 | 119,608 | **129,295** | **1.08×** |
| `gb_mega_throughput` | 10,571 | 48,259 | 66,921 | 78,140 | **74,862** | **0.96×** |

Repeats: as06 120k–129k, gb_mega 65.6k–74.9k. Official B200 worker will
differ. `events_per_sec` is `n_events / wall_clock_sec`.

## Championship roadmap (what 10× vs current would take)

A 10× from ~120k / ~78k is **~1.2M / ~780k ev/s**. That is not another
`__str__` or `isinstance` patch. After step 1 the Python tuple heap is
gone. Remaining wall is still **per-message Python**: `LimitOrder` /
`Message` objects, a numpy `RandomState` call per NoiseTrader draw, a
latency draw + ledger row per send, extract walking logs, and
MM/Value/Momentum `act()` still in Python. Discrete-event matching plus
Tier-A exactness forbids batched/JAX books.

**Keep in Python:** scenario JSON → config, rare paths (MarketHours,
post-close, modify/replace), parquet extract.

**Compile together:** book + kernel + all four Track-3 agents as one
C/Cython process with (1) compact event structs that still assign
`message_id` / `order_id` in ABIDES construction order, (2) a C heap
with the same `(deliver_at, (sid, rid, message))` / `Message.__lt__`
tie-break, (3) an RNG that matches `numpy.random.RandomState` bit-exact
(or a pre-drawn stream consumed in the same order), (4) a columnar
ledger written at deliver time.

**GPU:** only for *independent* `simulate-batch` scenarios. Never to
reorder one book's events.

**Tier-A risks:** heap-tie reordering; `message_id`/`order_id` construction
order; partial-fill snapshot vs in-place qty; `pipeline_delay` / latency
draws; STP `cancel_newest` / `cancel_oldest`; Kendall-τ if time
arithmetic changes.

**Suggested sequence:** (1) C event queue + compiled Exchange send/recv
while leftover agents still see Python objects; (2) compile Noise / MM /
Value / Momentum with bit-exact RNG; (3) zero-copy columnar ledger;
(4) GPU only for batch-of-scenarios.

## Championship step 1 (this change)

C `EventQueue` min-heap in `fast_sim._hotpath` (Python `heapq` twin in
`hotpath.py`). Each event is `(deliver_at, sid, rid, message_id)` plus a
borrowed `Message` pointer — the same key as
`(deliver_at, (sid, rid, message))` with `Message.__lt__` by
`message_id`. `Kernel.send_message` / `set_wakeup` / `runner` push and
pop that heap; leftover agents still receive Python `Message` /
`LimitOrder` objects. `message_id` / `order_id` assignment, numpy
`RandomState` draws, `pipeline_delay`, STP, and the partial-fill
snapshot are unchanged. GPU is unused.

**Family 1 14/14 exact.** as06, gb_mega, MP (`mp01`) and RA (`ra01`)
exact. Heap-order unit test matches `heapq` on
`(deliver_at, (sid, rid, message_id))`.

| Scenario | P5 | P6 | Step 1 best | vs Phase 5 |
|---|---|---|---|---|
| `as06_throughput_fast` | 119,608 | 129,295 | **138,985** | **1.16×** |
| `gb_mega_throughput` | 78,140 | 74,862 | **98,377** | **1.26×** |

Repeats: as06 130.8k–139.0k, gb_mega 95.9k–98.4k. Official B200 worker
will differ.

Post-step-1 cProfile (as06): `Kernel.run` still billed to the Python
caller (Cython loop). Next Python slices are extract, `order_executed`,
and leftover MM/Value `act()` (~3%). Agents are **not** the new wall —
do not rewrite them on this step.

## Championship step 2 (this change)

The remaining book wall after step 1 was Python `PriceLevel.peek` /
`pop` / `add_order` / `order_is_match` and `Side.is_bid()` called from
the already-compiled `execute_order` / `enter_order`. Step 2 inlines
those operations (same `_visible_qty` cache, same cheap-clone fill
snapshot) and compiles `cancel_order`. Agent-facing objects stay
Python `Message` / `LimitOrder`. No GPU. No agent rewrite.

**Family 1 14/14 exact.** as06, gb_mega, MP (`mp01`) and RA (`ra01`)
exact.

| Scenario | Step 1 | Step 2 best | vs Step 1 |
|---|---|---|---|
| `as06_throughput_fast` | 138,985 | **144,588** | **1.04×** |
| `gb_mega_throughput` | 98,377 | **103,585** | **1.05×** |

Repeats: as06 135.5k–144.6k (most runs sit in the step-1 band),
gb_mega 98.9k–103.6k. Official B200 worker will differ.

The PriceLevel Python bounce is gone. Leftover wall is still
`Kernel.run` billed to Python, extract, and agent callbacks. Agents
are **not** rewritten on this step.
