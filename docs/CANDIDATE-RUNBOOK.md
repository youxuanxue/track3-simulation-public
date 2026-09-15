# Local candidate qualification

A faster simulator is useful only if it still produces the right results and can
be delivered as the team's submission. These tools preserve the old image, test a
fixed new image, and retain the evidence behind every decision. Their results are
always local and non-rankable. A successful tool test does not establish B0 or B1;
those require the gates in [SUBMISSION-READINESS.md](SUBMISSION-READINESS.md).

Use Python 3.13 with `qfbench2-common[data]` pinned to `v2.4.1`. The simulator and
reference images retain their own pinned numerical stack. `scripts/preflight.sh`
checks the packaging and candidate-controller behavior before commits and pushes.

## Build and preserve image identity

`baselines/Dockerfile.candidate` reuses the immutable historical image and checks
that its compiled Cython sources match the checkout. It supports Python changes;
a native source change requires rebuilding the extensions with
`baselines/Dockerfile.native-candidate`, which preserves the fixed numerical runtime.
The reference image independently invokes the pinned ABIDES engine with all four
existing patches. Its extra logging dependencies are verified against committed
wheel hashes before installation.

```bash
python scripts/validate_candidate_parameters.py reference-wheels --out out/reference-wheels
docker build --platform=linux/amd64 -f baselines/Dockerfile.candidate -t ghcr.io/youxuanxue/track3-simulation-public:candidate .
docker build --platform=linux/amd64 -f baselines/Dockerfile.native-candidate -t track3-native-candidate .
docker build --platform=linux/amd64 -f baselines/Dockerfile.parameter-reference -t ghcr.io/youxuanxue/track3-simulation-public:parameter-reference .
```

After publication, use the registry digest in every command below. `IMAGE` and
`REFERENCE_IMAGE` denote complete `ghcr.io/...@sha256:...` references. Preserve
build logs and image manifests alongside experiment artifacts. Never replace the
historical digest with a mutable tag in an evidence file.

## G1 delivery

The candidate configuration declares the real website team number and phase.
The official toolkit derives the alias and constructs both ZIP members. Store the
real Team Key in an owner-only file outside the repository; supply its path, never
the key itself. Platform availability and a competition URL are not prerequisites.

```bash
python scripts/prepare_submission.py status --team-key-file /private/path/team-key
python scripts/prepare_submission.py verify-delivery --out out/delivery
python scripts/prepare_submission.py package --verification out/delivery/delivery.json --team-key-file /private/path/team-key --out out/development.zip
```

The package operation rechecks anonymous pulling, validates the C5 with the shared
parser and verifies the claim against the exact descriptor bytes. It refuses to
replace an existing ZIP. Delivery evidence is tied to the image and gate version;
the old partial public regression report cannot be used as delivery or G2 evidence.

## Independent parameters and full native runs

Freeze a new set before inspecting its results. Generation derives an `agent_mix`
from the published `agent_configs` because some public configs omit the metadata
required by the toolkit schema. It changes seeds and legal horizon/protocol
parameters and validates the generated scenarios using that schema. Parameter
comparison uses the shared semantic checks plus exact dataframe values and order.
A native exception probe also consumes RNG state and IDs before failing and checks
that fallback matches a clean Hybrid run.

```bash
python scripts/validate_candidate_parameters.py generate --seed 9152400 --reference-image "$REFERENCE_IMAGE" --out out/holdouts
python scripts/validate_candidate_parameters.py run --plan out/holdouts/holdout.json --image "$IMAGE" --out out/parameters
```

Create a JSON mapping from each measured image to the `path` and `sha256` of its
`heldout-result.json`, using `benchmark_candidates.index`. Supply it as `--holdout`.
The controller rechecks the raw differential evidence and complete case coverage.
A case record binds the candidate, reference image and frozen plan; every batch
subcase and both arms' raw outputs must be present. Validator source, fallback
probe and numerical library versions must still match. Relabeling an old result
for a different image or plan cannot qualify a candidate.
A failed set is regression evidence; implementation changes require a new holdout.

The qualification host must be native Linux/amd64. Use a dedicated scratch
filesystem with capacity at most 10 GiB for `--scratch-volume`; the runner makes
the image filesystem read-only and places both `/tmp` and `/output` on that bounded
filesystem. It samples its occupied bytes, records its enforced capacity, and uses
the existing cgroup memory sampler for total container memory. Output-file size
alone never qualifies disk usage. Missing telemetry blocks G2. Retained outputs
live outside scratch and need separate disk space.

```bash
python scripts/benchmark_candidates.py freeze --image "$IMAGE" --purpose baseline --round B0 --hypothesis 'Qualify the exact CPU baseline' --timeout 7200 --budget 18000 --holdout out/holdout-index.json --out out/b0-plan.json
python scripts/benchmark_candidates.py run --plan out/b0-plan.json --scratch-volume /mnt/t3-scratch --out out/b0
python scripts/benchmark_candidates.py assess --evidence out/b0/evidence.json
```

`--diagnostic` permits functional investigation on unsuitable machines, and can
never qualify G2 or G3. The optional native job in Track 3 CI records the actual
runner instance, executes independent parameters and then the selected measurement
on that same instance. It uses the existing public repository's standard runner;
no paid runner is configured. Its raw artifacts include failures.

Dispatch defaults to `measurement_purpose=screen`; choose `baseline` explicitly
for the complete protocol. A screen bounds each unit at two minutes and the
experiment at twenty minutes.
The native baseline job gives the full exemplar a longer per-unit deadline inside
its total experiment budget. These are local safety bounds recorded in the frozen
plan, not published official runtime limits.
Parameter artifacts are uploaded before simulation,
and each unit's start and final resource record also appear in the workflow log,
so a terminated worker does not erase all diagnostic evidence.
Every measurement stops after its first failed required invocation. This also
prevents another container from starting when a timed-out Docker client cannot
confirm that the previous container stopped. A failed host query ends the plan
with the query error and all previously indexed records in `evidence.json`.
Native jobs restore unresolved failure records from the latest retained artifact
on the same branch and execute sequentially. Missing or expired required history
stops qualification; a new runner cannot silently reset the failure count.
Confirmation reservations are also restored, including early artifacts from jobs
that stopped before uploading their final measurements. History lookup follows
pagination; an older confirmation cannot disappear beyond the first result page.

Large native workloads use a classification pass that stores one bit per execution,
then replay into bounded Parquet row groups. The classification retains only the
latest execution of orders whose future fills remain unresolved. It preserves the
original last-execution label even when fill and cancellation messages arrive out
of order. The second pass writes the complete trace and delivery ledger with exact
integer columns and nullable pandas metadata. Output buffers are bounded; the final
files must still fit the card's disk cap. This implementation alone does not prove
that the unchanged full exemplar fits that cap.

Single-scenario `throughput-scale` inputs omit the optional message ledger by
default. The complete transaction trace and actual kernel message count are
unchanged. Batch execution always emits its required per-subscenario ledgers,
including throughput batches; unknown single-scenario families also retain them.
`simulate --require-message-ledger` forces the throughput ledger for diagnostics.
The participant sees scenario JSON, not the organizer card. Consequently, an
organizer override requiring a throughput ledger must also be communicated through
the launch interface; the candidate cannot discover a hidden card override.
Local acceptance continues to use the scorer's card policy, so a missing required
ledger fails. Optional ledgers, when emitted, remain covered by repeat hashes and
parameter comparisons. Neither message delivery nor transaction rows are skipped.

The default shared sanitation limits used by the local retention call are a
separate bound from the 10 GiB scratch filesystem. At toolkit 2.4.1 they limit a
file to 64 MiB and the tree to 256 MiB. These are library defaults, not a verified
statement of the production Runner's deployed limits. An output refused by
retention cannot qualify; removing an optional ledger alone does not resolve a
transaction trace that exceeds these bounds.

Dispatch Track 3 CI with `build_native=true` to compile both extensions, run the bounded
streaming differential suite inside the image and export the tested modules with
source and file hashes. Assemble them on the pinned runtime and publish with an
existing registry credential; an Actions token may lack write access to an existing
package. The build job does not run G2/G3 or promote the image.

## Paired confirmation and candidate history

Use `freeze --purpose screen` for a bounded first check. It pins the shortest and
longest public horizon in each family, every batch and the unchanged full exemplar,
then runs one warm-up and one sample within the declared budget and per-unit timeout.
The complete roster remains recorded in the plan. Screening reports only a screen
verdict: it never produces a partial-roster score, a confidence interval, G2 or G3.
Even a successful screen requires a separately frozen complete confirmation.

For a challenge, freeze `--purpose confirmation --baseline "$B0" --image "$B1"`
with a new output path, the fixed hypothesis and both images' independent evidence.
The protocol warms each version independently, then alternates A/B order across
five complete paired groups. Counts are reread from parquet footers; the clock is
host-side. The bootstrap samples whole groups, jointly for A/B and every unit,
and recomputes each unit's median and the complete-roster mean. It reports family
intervals and the worst unit delta. Screening data cannot establish confirmation.
A candidate's confirmation is reserved once in the history directory at launch;
failed or interrupted confirmation data must be retained, not repeatedly sampled
until the interval is positive.

The native Actions job accepts `measurement_purpose=confirmation`, `baseline_image`,
`candidate_image`, `reference_image`, and the predeclared `hypothesis`. Both images
run the same fresh parameter set and the paired measurement on that job's instance.
The job exports its candidate-bound reservation and requires its artifact upload
to succeed before the first measured invocation. A later job restores that record
and refuses another confirmation for the same candidate, even if the earlier job
was interrupted. An expired reservation requires recovery; it does not grant a
new opportunity. The job requires G3 to pass for confirmation success and retains
the evidence for rejected or inconclusive candidates.

```bash
python scripts/benchmark_candidates.py decide --evidence out/b0/evidence.json --g1 out/development.g1.json --history out/candidates
python scripts/benchmark_candidates.py decide --evidence out/confirmation/evidence.json --g1 out/b0.g1.json --g1 out/b1.g1.json --expected-stable "$B0" --history out/candidates
python scripts/benchmark_candidates.py rollback --to "$B0" --expected-stable "$B1" --reason 'Observed regression with retained reproducer' --history out/candidates
```

History is append-only with a hash chain and a locked compare-and-set of the stable
image. Missing evidence holds the old candidate; failures reject the challenge;
only the full gates promote it. Rollback retains the failed version's history and
requires the earlier candidate's current G2 and intact G1 package. Exemplar semantic
truth remains `unknown` even when its public checks and complete execution pass.
Because each complete pair requalifies both images, rollback uses the latest
qualified evidence containing its target. This allows B0's fresh paired evidence
to replace its original baseline proof when the controller source has changed;
the selected evidence still undergoes the full current G2 check.
