"""Markers and named skips for the Docker/GPU integration suite.

Global rule 6: unit tests must be hermetic, and anything touching Docker, a network, a GPU or a
private sibling repository is an explicit integration test. This directory is that suite, and it is
kept OUT of `tests/` proper so `python -m pytest tests/ --ignore=tests/integration` (and the
secret-free CI job, which runs the guards by file) can never pull a daemon call in by accident.

The measured reason this exists: `tests/test_run_unit.py::test_reclaim_output_is_best_effort_and_
never_raises` called an un-mocked helper that shelled out to a real `docker run`. It was the
slowest test in the suite at 1.47 s of 3.15 s total, and on a networked runner it attempted a
registry pull. The helper it exercised has since been deleted; the structural fix — a separate,
marked suite — stays.

**Every skip here names its reason.** "Skipped because Docker is not installed" is a fact an
operator can act on; a silent pass is not.

    python -m pytest tests/integration -m integration          # opt in
    python -m pytest tests/ --ignore=tests/integration         # the hermetic suite
"""

from __future__ import annotations

import shutil
import subprocess

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "integration: needs Docker, a GPU or a real participant image"
    )


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        proc = subprocess.run(
            ["docker", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _nvidia_available() -> bool:
    return shutil.which("nvidia-smi") is not None


@pytest.fixture(scope="session")
def docker() -> None:
    if not _docker_available():
        pytest.skip(
            "no reachable Docker daemon on this host; the timed-run integration tests need one. "
            "Run them on a worker-shaped machine, not in the hermetic unit suite."
        )


@pytest.fixture(scope="session")
def gpu() -> None:
    if not _nvidia_available():
        pytest.skip(
            "no nvidia-smi on this host, so GPU telemetry cannot be exercised. The frozen C7 "
            "requirements (50 ms sampling, coverage >= 0.95, UUID-resolved attribution) can only "
            "be validated on the pinned instance; a local green run does not close A10/A20."
        )
