# T3 Submission Readiness

The accelerated simulator has passed local public correctness checks. An official
score still requires a reachable competition page and the organizer's team-ID
mapping. The preparation tool keeps those two states separate and packages only
the exact image that was verified.

## Candidate And Evidence

- [Candidate configuration](../submission/candidate.json) owns the immutable image
  reference, submission license, and confirmed registration fields.
- [Local verification](../submission/validation.json) records the public regression,
  batch isolation, and repeated single/batch parquet checks. It is a developer
  result (`rankable=false`), not an official score or a speedup claim.
- [Implementation](../baselines/fast_sim/) uses a native C/Cython CPU path with the
  pinned ABIDES adapter and hybrid fallback. No model endpoint or GPU setup is needed.

The root `submission.json` is legacy participant metadata, **not a valid C5 upload
descriptor**. The old workspace ZIP used the unconfirmed identity `youxuanxue`.
Do not upload that ZIP. Generate a new one with the command below after obtaining
the organizer's mapping. Registry owner, website team ID, and CodaBench username
are different identifiers.

## Remaining Work

| Item | Current Evidence | Completion Condition |
| --- | --- | --- |
| Official T3 Development URL | No link in authenticated Resources or Announcements on 2026-09-08; CodaBench title searches found no Agenthon/QFBench/Alphathon competition | Obtain the organizer's T3 Development page and enter it |
| C5 team identity | Website team `chilli`, Team ID `23`, CodaBench username `chilli`; mapping unknown | Record the exact assigned `confirmed_c5_team_id` and its source URL |
| Large exemplar | No public reference trace; the extra full-size local run was stopped without a verdict | Complete a resource-bounded run on suitable native amd64 hardware; ask the official scorer for semantics |
| Official timing and held-out cases | Local runs use macOS ARM with amd64 emulation; no official submission ID | Submit Development, retain per-unit results, diagnose any failing gates, and rerun after fixes |
| Final submission | Final is a separate, one-submission phase | Recheck current official rules, toolkit and image pins before preparing Final; this tool generates Development only |

The [organizer's identity clarification](https://github.com/Agenthon-2026/Agenthon2026-public/issues/3#issuecomment-5534947498)
explicitly says C5 `team_id` is not derived from the website Team ID. Its competition
publication statement concerns Track 1; the T3 availability observation above is
our own authenticated website/CodaBench check, not an organizer promise.

Keep the Team Key in the authenticated website session. Never put it in this
repository, `candidate.json`, evidence, a PR, or the ZIP. For browser operations,
reuse the user's logged-in native Chrome tabs.

## Reproduce Locally

From this repository, with Docker and Python 3.13+ installed:

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install \
  'qfbench2-common @ git+https://github.com/Agenthon-2026/Agenthon2026-public.git@v2.3.1#subdirectory=common' \
  pandas pyarrow pytest ruff==0.4.7
git lfs pull
PYTHON=.venv/bin/python bash scripts/preflight.sh
.venv/bin/python scripts/prepare_submission.py status
.venv/bin/python -u scripts/prepare_submission.py verify --out out/development-check
```

`status` exits 1 while registration fields are missing. Code preflight may pass in
that state; it checks the tool and its refusal behavior, not competition availability.

`verify` requires a **new** output directory, probes GHCR anonymously, pulls by
digest, validates public manifests, builds the reference cache, and runs the public
regression and batch gates. It compares repeated parquet bytes for a single scenario
and a batch. Container runs use no network, four CPUs, and a 16 GB memory limit;
the batch/repeat timeout is a local 1,800-second safeguard, not an official cap.
Docker Desktop must share the output directory. Logs and large traces stay under
the gitignored `out/`; `verification.json` is the compact evidence artifact.

The exemplar is intentionally outside this gate because it has no public reference
trace. A public-regression pass therefore never claims all-unit or held-out success.

## Package And Submit

Fill the three registration fields in `submission/candidate.json` from the official
page. The tool validates their presence and HTTPS shape; a URL alone cannot prove
that the organizer assigned an ID, so check the cited source before filling them.

```bash
.venv/bin/python scripts/prepare_submission.py status
.venv/bin/python scripts/prepare_submission.py package \
  --verification out/development-check/verification.json \
  --out out/t3-development.zip
```

The package contains only a sealed C5 `submission.json`, derived from the installed
toolkit fixture. It refuses incomplete registration, incomplete local evidence,
another image digest, or an existing destination ZIP. Local evidence is a
participant record, not signed organizer telemetry. Nothing here uploads or uses
a submission quota automatically.

Upload on the official Development page using native Chrome. Preserve the
submission ID, image/descriptor digests, queue status and final gate report; keep
authentication material and any sealed data out of public artifacts. Only that
platform result can establish official validation.
