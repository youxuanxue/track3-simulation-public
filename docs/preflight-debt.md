# Preflight Coverage

Repository CI and `scripts/preflight.sh` cover the participant preparation tool.
Some checks still require infrastructure unavailable to the public test runner.

| Gap | Current Check | Resolution |
| --- | --- | --- |
| Shared dev-rules hooks are not installed in this fork | Explicit preflight before commits/pushes and the PR CI job | Integrate the managed rules/hook distribution when onboarding this fork; this PR does not vendor a second rules copy |
| Docker correctness and registry reachability are not PR unit tests | `prepare_submission.py verify` performs real image runs and writes evidence | Run before each candidate-image submission, and after toolkit/scoring changes |
| Official identity and competition availability | `status` and `package` reject missing registration fields | Resolve from the organizer's published page before the first Development upload |
| Large exemplar and official hardware measurements | Named as unverified in the readiness document and verification artifact | Resolve on suitable amd64 hardware and the official evaluator before Final |
