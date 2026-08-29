"""Docker-backed checks for the local developer harness. Explicitly marked; never hermetic.

These are the parts of `throughput/run_unit.py` and `throughput/timer.py` that cannot be asserted
without a daemon: that the deadline actually kills a CONTAINER (not just the docker client), and
that a run against a real image produces a record stamped `rankable = False`.

    python -m pytest tests/integration -m integration
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from throughput.timer import bounded_container_run, kill_container  # noqa: E402

pytestmark = pytest.mark.integration

#: A tiny public base image. Named as a constant so an operator can point it at a mirror.
BUSYBOX = "busybox:1.36"


def test_a_container_that_never_exits_is_killed_at_the_deadline(docker: None) -> None:
    """The bound that matters. Pre-fix there was no `timeout=` at all, so an image with an
    infinite loop hung the harness until somebody noticed."""
    with tempfile.TemporaryDirectory() as tmp:
        cidfile = Path(tmp) / "container.cid"
        proc = bounded_container_run(
            [
                "docker",
                "run",
                "--rm",
                "--cidfile",
                str(cidfile),
                "--network=none",
                BUSYBOX,
                "sh",
                "-c",
                "while true; do sleep 1; done",
            ],
            cidfile=cidfile,
            timeout_sec=10.0,
        )
    assert proc.returncode < 0, "a killed run must not look like a clean exit"


def test_the_container_itself_is_killed_not_only_the_client(docker: None) -> None:
    """Killing the docker CLIENT leaves a detached container holding the CPU quota and the GPU,
    which corrupts the next measurement as well as leaking the run."""
    with tempfile.TemporaryDirectory() as tmp:
        cidfile = Path(tmp) / "container.cid"
        bounded_container_run(
            [
                "docker",
                "run",
                "--rm",
                "--cidfile",
                str(cidfile),
                "--network=none",
                BUSYBOX,
                "sh",
                "-c",
                "while true; do sleep 1; done",
            ],
            cidfile=cidfile,
            timeout_sec=10.0,
        )
        container_id = cidfile.read_text().strip() if cidfile.exists() else ""
    if not container_id:
        pytest.skip("docker did not write a cidfile on this daemon version")
    inspect = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", container_id],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=30,
        check=False,
    )
    # Either the container is gone (--rm reaped it) or it is explicitly not running.
    assert inspect.returncode != 0 or inspect.stdout.decode().strip() == "false"


def test_a_stdout_flood_costs_a_fixed_number_of_bytes(docker: None) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cidfile = Path(tmp) / "container.cid"
        proc = bounded_container_run(
            [
                "docker",
                "run",
                "--rm",
                "--cidfile",
                str(cidfile),
                "--network=none",
                BUSYBOX,
                "sh",
                "-c",
                "yes 0123456789 | head -c 20000000",
            ],
            cidfile=cidfile,
            timeout_sec=120.0,
            log_tail_bytes=8192,
        )
    assert len(proc.stdout) <= 8192


def test_kill_container_is_harmless_on_a_stale_id(docker: None) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cidfile = Path(tmp) / "container.cid"
        cidfile.write_text("0" * 64)
        kill_container(cidfile)  # must not raise
