# Track 3 — Simulation Engine Baselines

This document describes the performance baselines that define the admissibility and ranking
thresholds for Track 3 submissions. The canonical timing protocol is `throughput/timer.py`.

> **The throughput figures below have not been re-measured on the fleet.** They were written on
> 2026-06-23, when this repository was created and the benchmark hardware was still described as
> "4× AMD EPYC vCPU" with an unnamed GPU. The hardware in §3 was measured on 2026-08-20 and the
> figures were not: they predate both the B200 hosts and the gVisor sandbox every ranked run now
> executes under. Read every events/sec number here as **indicative of scale, not as a measurement
> of this fleet** — including the ABIDES floor. Re-measurement is tracked in
> [#42](https://github.com/Agenthon-2026/track3-simulation-public/issues/42).

---

## 1. Baseline 1: Unmodified ABIDES Python Stack

**Source:** https://github.com/jpmorganchase/abides-jpmc-public
**License:** BSD-3-Clause (see full text at the link above)
**Pinned commit:** `f9cbe51342b7dedd9587e4e069040d68a5c6477f`

This single commit SHA is the canonical Track 3 baseline pin. It is the value that
every `abides_fork_commit` field in the track (unit `card.toml`, `manifest.json`,
and the sealed `index.json` files) must carry. The baseline is referenced by this
pinned upstream commit rather than vendored as a git submodule; there is no
`baselines/abides-python/` checkout in this repository.

### What it is

ABIDES (Agent-Based Interactive Discrete Event Simulation) is the open-source
multi-agent financial market simulator released by J.P. Morgan. It drives a
limit-order-book (LOB) via a discrete-event queue with heterogeneous agent archetypes
(market makers, momentum traders, noise traders, etc.). The Python implementation is
single-threaded and event-driven; each event invocation calls back into pure-Python
agent logic, which makes it straightforward to extend but imposes significant
per-event overhead.

### Throughput

| Hardware | Observed throughput | Measured on this fleet? |
|---|---|---|
| 4 vCPU (x86-64), 16 GiB RAM, GPU unused (CPU-only baseline) | ~50,000–80,000 events/sec | **No** — 2026-06-23, hardware since replaced |
| Geometric mean over public regression scenarios | ~65,000 events/sec | **No** — same provenance |

This document has named `timer.py --runs 5 --discard-warmup` as the protocol for these figures
since 2026-06-23, which is when the table was written. No run of it on the fleet is recorded, so
what the numbers describe is the hardware this repository described at the time — **not** the B200
hosts in §3, and **not** the gVisor sandbox every ranked run executes under.

The wide range (50 k–80 k) reflects scenario complexity: scenarios with many active agents and
complex order-book states run slower than sparse, low-agent-count scenarios.

**The reference traces shipped in this repository point one way.** Each of the 65 public units
carries an `events.json` written by `baselines/abides_fork/simulate.py` from the pinned ABIDES fork
— the same baseline this table describes. Their measured `events_per_sec` runs from **3,471 to
18,046, geometric mean 13,793**: about **4.7× below** the 65,000 figure above.

The hardware those runs used is not recorded and `wall_clock_sec` there covers the simulation loop
rather than the whole container, so they do not replace a `timer.py` run on the fleet. They are
also not explained by the sandbox: per §3 a Python event loop is nearly free under gVisor
(allocation 0.3%, heap −0.9%, i.e. noise). Treat 65,000 as the number least likely to be right, and
see [#42](https://github.com/Agenthon-2026/track3-simulation-public/issues/42).

### The `simulate` adapter

Stock ABIDES has no `simulate` CLI that reads a Track 3 `scenario.json` and emits the
canonical `trace.parquet`. That verb is provided by the **`abides_fork` adapter** in
this directory (`baselines/abides_fork/`), layered on the unmodified pinned engine:

- `config.py` — maps a `scenario.json` (`exchange_config` / `oracle_config` /
  `latency_config` / `agent_configs`) onto an ABIDES config (mirrors `rmsc04`).
- `agents.py` — lightweight scheduled traders (`NoiseTrader`, `MarketMaker`,
  `ValueTrader`, `MomentumTrader`) that produce the order flow each scenario describes.
- `trace.py` — maps ABIDES event logs onto the canonical 7-column trace schema.
- `simulate.py` — the CLI: runs the scenario, writes `trace.parquet` + `events.json`.

### Pre-built baseline image (recommended)

The baseline ships as a Docker image built from the `Dockerfile` in this directory. It
fetches ABIDES at the pinned commit, applies the pomegranate-removal patch (`patches/`),
installs the engine and the adapter, and exposes the `simulate` verb the interface
contract below requires. Build it and validate it against the public reference traces
(the same harness path the evaluator uses) with:

```bash
./baselines/build_and_validate.sh           # docker build + regression_suite/run_regression.py
```

### Installing and running locally (without Docker)

Run from the repository root. **The ABIDES baseline is the one deliberate exception to the org's
Python 3.13 standard**: ABIDES requires pandas 1.x, and that pinned stack (`numpy==1.26.4`,
`pandas==1.5.3`) publishes no cp313 wheels, so the baseline stays on 3.11 — it is the exact stack
that generated the frozen reference traces. It is self-contained: the harness only *reads* the
parquet it writes, so it does not constrain the 3.13 evaluation container or your own submission
image.

```bash
# Clean Python 3.11 environment with the pinned stack (baseline exception — see above;
# coloredlogs is a runtime import of abides_core.abides and is not declared in ABIDES's setup.cfg)
python -m venv .venv && source .venv/bin/activate
pip install numpy==1.26.4 pandas==1.5.3 scipy==1.17.1 pyarrow==15.0.2 coloredlogs==15.0.1

# Fetch ABIDES at the pinned commit and drop the unbuildable pomegranate dependency
git clone https://github.com/jpmorganchase/abides-jpmc-public
git -C abides-jpmc-public checkout f9cbe51342b7dedd9587e4e069040d68a5c6477f
git -C abides-jpmc-public apply "$PWD/baselines/patches/order_size_model.pomegranate-free.patch"
pip install --no-deps abides-jpmc-public/abides-core abides-jpmc-public/abides-markets

# Run a public regression scenario through the adapter
PYTHONPATH=baselines python -m abides_fork.simulate \
    --config regression_suite/scenarios/s001_price_time_priority.json \
    --out /tmp/baseline_trace.parquet
```

This writes `trace.parquet` and an `events.json` sidecar next to `--out`. Always check
out the exact pinned commit above to reproduce the baseline traces.

### Interface contract

**All Track 3 submissions must honour this CLI interface exactly.** The evaluation
harness calls your Docker image with:

```
simulate       --config /input/scenario.json --out /output/trace.parquet
simulate-batch --batch-dir /input/scenarios   --out-dir /output
```

- `/input/scenario.json` — read-only bind-mount of the scenario configuration.
  Schema: `common/schemas/sim_scenario.schema.json`.
- `/output/trace.parquet` — the full event trace, one row per market event.
  Must conform to the column spec in `common/schemas/sim_scenario.schema.json §outputs`.
- `/output/message_trace.parquet` — message-level kernel ledger. Required by the
  exchange-protocol, reactive-agent, and batch units; feeds the latency/causality and
  g3.5 message-ledger checks.
- `/output/events.json` — aggregate summary written alongside the trace. The `events`
  schema in `common/schemas/sim_scenario.schema.json` requires **all six** keys:
  `scenario_id` (str), `n_events` (int), `wall_clock_sec` (number), `events_per_sec`
  (number), `seed` (int), `trace_sha256` (64-hex str). (The g1 schema gate rejects an `events.json`
  missing any of the six.) **`n_events` must equal the row count of `trace.parquet`** (for
  `simulate-batch`, `total_events` must equal the sum of the per-sub trace row counts). The harness
  counts events itself from the emitted trace and uses that count as the numerator of the ranked
  `events/sec`; the declared value is cross-checked against it and a mismatch fails the run, so an
  over-declared `n_events` cannot raise a submission's throughput.
  It now also carries two telemetry keys — `peak_memory_bytes` (int) and `gpu_seconds`
  (number) — consumed by the secondary diagnostics (see §3).

The `simulate-batch` verb (BatchMarketSim) runs the sub-scenarios under `/input/scenarios`
in one pass, writing `/output/<sub>/{trace,message_trace}.parquet` + `events.json` per
sub, plus an aggregate `/output/batch_events.json`
(`n_scenarios` / `total_events` / `events_per_sec` / `per_scenario`). Batch units
(`t3-gbatch-*`) rank on the aggregate throughput reported in `batch_events.json`.

Submissions that exit non-zero, fail to write `trace.parquet`, or write an
`events.json` missing any required key will be marked `shared.schema.invalid_output`
and `t3.parse_error`, and will not receive a throughput score.

---

## 2. Baseline 2: Vectorized Reference Simulator (Interface Stubs)

This is an in-house NumPy-vectorized LOB simulator developed by the QFBench team.
It is **not open-sourced** and is not available to participants. It serves two purposes:

1. **Admissibility pass reference:** the harness cross-checks that candidate output
   traces are semantically equivalent to this simulator's output on the regression
   scenarios before awarding a throughput score.
2. **Internal performance reference point:** it is how the organizers sanity-check what the
   scenarios cost a well-optimized implementation. It does **not** enter the score — there is no
   normalization step for it to be the ceiling of (see the note under the table below).

### Throughput

| Hardware | Observed throughput | Measured on this fleet? |
|---|---|---|
| 4 vCPU (x86-64), 16 GiB RAM, GPU unused (CPU-only baseline) | ~400,000 events/sec | **No** — 2026-06-23, hardware since replaced |

Achieving or exceeding this figure is not required and earns nothing. **There is no normalized
throughput score.** Ranking is by raw median `events_per_sec` descending (`LEADERBOARD_SORT =
"desc"`); no transform maps a rate onto a 0-1 mark, and this figure is not the top of any scale.
An earlier revision of this page said submissions in this range "receive maximum normalized
throughput marks" and called this simulator the "upper reference point in the scoring
normalization" — there is no such normalization in the scorer. Treat the number as orientation
only, and note that it has never been reproduced on the evaluation hardware.

### Public interface stub (Python 3.13)

Participants may implement any internal architecture — vectorized NumPy, Numba JIT,
compiled Cython extensions, Rust via PyO3, a compiled C extension, etc. — as long as
the Docker CLI interface and the output schema described in §1 are preserved. The
following Python stub documents the logical interface that the reference simulator
satisfies:

```python
from pathlib import Path
import pandas as pd

class VectorizedLOBSimulator:
    """
    Vectorized limit-order-book simulator interface.

    Parameters
    ----------
    scenario_config : dict
        Parsed contents of the scenario JSON (matches sim_scenario.schema.json).
    seed : int
        Random seed for reproducible agent behaviour. Must be a non-negative
        integer < 2**31 (enforced by the seed-derivation protocol in timer.py).
    """

    def __init__(self, scenario_config: dict, seed: int) -> None:
        """Initialise simulator state from config and seed. Must not perform I/O."""
        ...

    def run(self) -> tuple[pd.DataFrame, dict]:
        """
        Execute the full simulation and return results.

        Returns
        -------
        trace_df : pd.DataFrame
            Full event trace. Columns must match the spec in
            common/schemas/sim_scenario.schema.json §outputs.columns.
        events_dict : dict
            Aggregate summary. Must contain at minimum:
              - "n_events": int       — used by timer.py for events/sec
              - "scenario_id": str    — echoed from config for traceability
              - "seed": int           — the seed used
        """
        ...
        # Returns (trace_df, events_dict)
```

The `simulate` CLI entry point in compliant submissions is expected to:

1. Parse `/input/scenario.json` into a `dict`.
2. Extract the `seed` field (or fall back to `scenario_config["base_seed"]`).
3. Instantiate the simulator, call `.run()`, and serialise outputs.

---

## 3. Performance Reference Points

Ranking is by **raw median `events_per_sec`** on the sealed throughput-scale scenarios,
descending — there is no normalized-score transform. The ABIDES baseline is the throughput
floor: a submission whose median throughput does not exceed it is admissible but unranked
(`t3.throughput_nonimproving`). The vectorized reference is an internal performance reference
point only and gates nothing.

| Configuration | events/sec (geomean) | Provenance | Role |
|---|---|---|---|
| **Unmodified ABIDES baseline** | ~65,000 | **Not measured on this fleet.** Written 2026-06-23 against the hardware this repository described at the time. | Ranking floor (`t3.throughput_nonimproving` at or below) |
| **Unmodified ABIDES baseline** | 13,793 | **Measured**, from the `events_per_sec` in the 65 shipped public `units/*/events.json`, all written by the pinned baseline. Range 3,471–18,046. Hardware not recorded, and `wall_clock_sec` there covers the simulation loop rather than the whole container. | Not a threshold — the only reproducible baseline number in this repository |
| **Vectorized reference** | ~400,000 | **Not measured on this fleet**, same 2026-06-23 provenance. | Internal reference (not a gate) |

**Read the two baseline rows together, and treat neither as a target.** They disagree by about
4.7×, on the same engine, and the gap is not explained by the sandbox (per §3 a Python event loop
is nearly free under gVisor: allocation 0.3%, heap −0.9%, i.e. noise). The 65,000 figure is the one
least likely to be right. Re-measurement is tracked in
[#42](https://github.com/Agenthon-2026/track3-simulation-public/issues/42); until it lands, no
number on this page is a measurement of the fleet your submission will be scored on.

A row reading "typical competitive submission: 150,000–600,000 events/sec" used to sit in this
table. It was removed rather than re-qualified: no such range was ever measured, there were no
submissions to measure it from, and a fabricated band is worse than no band — it invites tuning
towards a number that means nothing.

**What the floor is actually compared against — and it is not 65,000.** The
`t3.throughput_nonimproving` label is decided by comparing the median of your throughput units'
`events_per_sec` against the median of the **`events_per_sec` recorded in the reference
`events.json` of those same units**. Those are the frozen numbers the pinned baseline emitted when
the reference traces were generated — for the public units, the 3,471–18,046 range in the second
row above. The floor is not re-measured on the evaluation box, and it is not read from any table
on this page.

That is also why this label is informational rather than disqualifying: it compares a
host-measured rate against a rate frozen on unrecorded hardware, so it says "your run was not
faster than the recorded baseline run", not "your simulator is slow". You still receive a rank.

**Official benchmark hardware:**

Measured on the fleet 2026-08-20, not quoted from a spec page. Every host is identical, and
this is your compile target for the whole competition.

**What your container gets** (the caps on every Track 3 unit card):

- CPU: **4 vCPU**, `cpus = 4`
- RAM: **16 GiB**, `memory = "16G"`
- Disk: 10 GiB
- GPU: one **NVIDIA B200**, attached to every timed run
- Network: disabled at runtime (`network = "none"`) — the CUDA runtime and every other
  dependency must be vendored into your image

**The host underneath** (larger than your caps; listed so you know the microarchitecture you are
compiling for, not as a resource budget):

| | |
|---|---|
| GPU | NVIDIA **B200**, compute capability **10.0**, 183359 MiB, ECC and persistence mode on |
| Driver | **580.173.02** |
| CUDA toolkit on host | **13.0.3** (driver supports up to 13.0) |
| CPU | **Intel Xeon Platinum 8570** |
| OS | Ubuntu 24.04.4 LTS, kernel 6.11.0-1016-nvidia |
| Container stack | Docker 29.7.2, gVisor `release-20260803.0`, nvidia-container-toolkit 1.19.1-1 |

Inside a task container you will see kernel `4.19.0-gvisor`, the B200 with all 183359 MiB, and
driver 580.173.02.

### What the sandbox costs, by workload shape

Your run is sandboxed under gVisor. That is not a flat tax, and knowing where it falls is the
difference between optimising the right thing and the wrong thing for a week.

Measured by NVIDIA on 2026-08-25 under a real Track 3 card's caps (`--cpus=4 --memory=16G`), five
repeats on two hosts, wall-clock throughout, stable to within a point across hosts:

| workload shape | `runsc-gpu` | `runc` | sandbox cost |
|---|---|---|---|
| CPU-bound arithmetic | 46,644,253 | 50,084,398 | **6.9%** |
| allocation churn | 11,951,054 | 11,989,250 | **0.3%** |
| heap operations | 7,786,441 | 7,716,932 | **−0.9%** (noise) |
| raw syscalls | 718,093 | 4,755,762 | **84.9%** |
| loopback socket IPC | 281,129 | 1,105,189 | **74.6%** |

**GPU work has no measurable steady-state penalty** — 0.0218 s against 0.0217 s on a repeated
matmul. The only GPU cost is context creation on the first CUDA call, which varied between +65 ms
and +365 ms across hosts. That lands inside your timed window, so pay it once and reuse the
context rather than creating one per scenario.

**The shape of this is good news for a discrete-event simulator.** Object churn and heap operations
— the bulk of a Python event loop — are free. What is expensive is precisely what gVisor
intercepts: raw syscalls and loopback IPC.

So the guidance is specific rather than vague. **The sandbox does not tax your event loop, your
allocations, or your GPU work. It taxes syscalls and IPC.** A design that batches writes and avoids
a syscall per event pays almost nothing for running sandboxed. One that treats syscalls as free —
per-event logging, a socket between worker processes, an `fsync` in the hot path — loses most of
its speedup to the sandbox rather than to its own algorithm, and will read as an algorithmic
disappointment when it is not one.

An earlier figure of "~9% overhead" circulated between us and NVIDIA. It came from a spin loop,
which measures only the first row of that table; treat it as superseded.

> ### CUDA 12.x images work. Do not rebuild for 13.x on our account.
>
> The driver tops out at CUDA 13.0 and is **backward compatible**, so a 12.x image runs fine.
> Verified on this hardware: `cupy-cuda12x` JIT-compiles for `sm_100` against a CUDA 12.8 base.
> The GPU starter image we ship is itself 12.x. The 13.0.3 figure above is what the host happens
> to carry — it is **not** a requirement, and reading it as one would send you rebuilding for
> nothing.

Compile for `sm_100`. A PTX-only build will JIT on first launch, which lands inside your timed
window; ship cubins for `sm_100` if that matters to you.

A GPU is available to every submission; using it is **optional**. Track 3 is ranked on raw
events/sec and nothing else, so a well-optimized CPU simulator competes on equal terms — the
device is an opportunity, not a requirement. Note that the admissibility gates (Tier-A exact
fills, Kendall-τ ≥ 0.999, message-ledger causality) punish approximation, and discrete-event
simulation resists batching, so a GPU port is not a free win.

Track-3 timing runs on the shared fixed-SKU box, with a node-fingerprint fairness rule
enforced across submissions (per the organizers' evaluation-infrastructure policy, which is
held in an organizer-only repository and not published here). Alongside the
raw-throughput rank, the harness reports **secondary diagnostics** — speedup (vs. the
CPU-ABIDES baseline), efficiency (events/sec per GPU- or CPU-core-hour), and memory
efficiency (events per peak resident byte) — derived from the `events.json`
`peak_memory_bytes` / `gpu_seconds` telemetry. These are reported, not ranked.

### Throughput classification labels

These are the canonical `FailureLabel` values (see
`common/qfbench2_common/failure_labels.py`):

| Condition | Label | Ranked? |
|---|---|---|
| events/sec > ABIDES baseline geomean | (no label; normal) | Yes |
| events/sec ≤ ABIDES baseline geomean | `t3.throughput_nonimproving` | No — admissible but unranked |
| Output dir/trace/`events.json` missing or malformed | `t3.parse_error` (+ `shared.schema.invalid_output`) | No — inadmissible |
| Semantic equality check fails (Tier A fill sequence or Tier B proximity) | `t3.semantic_regression_fail` | No — inadmissible |
| Stylized-fact ceiling breached (Family 5) | `t3.stylized_fact_breach` | No — inadmissible |
| Sealed reference trace fails its SHA-256 checksum | `t3.reference_integrity_error` | Evaluation halted (operator-side) |

`t3.throughput_nonimproving` is **not a disqualifying failure**: the submission is
admissible (it ran cleanly and produced correct output) but will not appear in the
ranked throughput leaderboard. Participants whose primary contribution is semantic
accuracy rather than speed may still receive marks from other scoring dimensions.

---

## 4. Dependency Constraints

The evaluation container is a fixed image; participants must vendor all
non-standard dependencies inside their own Docker image. The following packages
are **pre-installed** in the evaluation container and do not need to be vendored:

| Package | Minimum version |
|---|---|
| Python | 3.13.x |
| NumPy | 1.26.x |
| Pandas | 2.2.x |
| SciPy | 1.13.x |

The interpreter is **pinned at 3.13**, not a floor you may exceed: `nemoguardrails` and `nvidia-nat`
both pin `<3.14`, so 3.13 is the newest version the org can run. Like the rest of the box spec, this
pin is part of the hardware contract published before the **2026-08-10 compute-caps freeze** — it
will not move under you mid-competition.

The following packages are **allowed** but **must be vendored** (not assumed present):

| Package | Notes |
|---|---|
| Numba | Include full conda/pip install in your Docker image; LLVM is large — plan image size accordingly |
| CuPy | A GPU is attached to every timed run — build against the published CUDA toolkit version (§3) |
| JAX | CPU-only JAX is pre-tested; for GPU JAX, pin the wheel to the published CUDA version (§3) |
| Cython / compiled extensions | Must be compiled for `linux/amd64`; build in your Dockerfile |
| Rust extensions (via PyO3/maturin) | Must be compiled for `linux/amd64`; multi-stage builds recommended |
| Any other package | Vendor it; the network is disabled at runtime |

**Network is disabled at runtime** (`docker run --network=none`). Any attempt to
make outbound connections will fail silently. Submissions must not depend on remote
model weights, API calls, or package downloads at inference/simulation time.

**Image size guidance:** the evaluation harness pulls images from the registry once
per evaluation run and caches them. Images larger than 8 GiB will be accepted but
may incur a pull-time penalty that is excluded from the throughput measurement. Keep
your image lean: use multi-stage builds, strip debug symbols from compiled artifacts,
and avoid bundling unnecessary large models.
