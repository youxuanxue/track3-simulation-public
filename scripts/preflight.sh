#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
python_bin="${PYTHON:-python3}"
git diff --check
git diff --cached --check
"$python_bin" -m ruff check scripts/prepare_submission.py tests/test_submission_preparation.py
"$python_bin" -m ruff format --check scripts/prepare_submission.py tests/test_submission_preparation.py
"$python_bin" .github/validate_units.py simulation --stdlib-only
"$python_bin" -m pytest tests/test_submission_preparation.py -q
