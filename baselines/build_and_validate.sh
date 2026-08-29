#!/usr/bin/env bash
# Build the Track 3 baseline image and validate it against the shipped public
# reference traces using the regression harness — the same path the evaluator uses.
#
#   ./baselines/build_and_validate.sh [image_tag]
#
# Requires: docker (with buildx for --platform on non-amd64 hosts) and a Python
# env with qfbench2-common + numpy/pandas/scipy/pyarrow installed (for the harness).
#
# A clean run prints PASS for every public scenario. Because scoring is semantic
# (Tier A exact fills + stylized-fact proximity), the linux/amd64 image must
# reproduce the reference traces' fill sequences; if a Tier-A mismatch appears,
# regenerate the references inside this image and re-run.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # baselines/
REPO="$(cd "$HERE/.." && pwd)"                          # track3-simulation-public/
IMAGE="${1:-track3-abides-baseline:latest}"

echo ">>> Building $IMAGE (linux/amd64) ..."
docker build --platform=linux/amd64 -t "$IMAGE" "$HERE"

echo ">>> Running regression harness against $IMAGE ..."
python "$REPO/regression_suite/run_regression.py" \
    --candidate-image "$IMAGE" \
    --scenarios-dir "$REPO/regression_suite/scenarios" \
    --reference-dir "$REPO/regression_suite/reference_traces" \
    --output-dir "$REPO/regression_suite/run_outputs" \
    --workers 3
