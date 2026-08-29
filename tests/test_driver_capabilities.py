"""gVisor will not start a GPU sandbox unless the driver capabilities are declared.

Measured by NVIDIA on a B200 (2026-08-18): under `runsc-gpu`, `--gpus all` alone fails with
exit 125 -- the sandbox never starts -- and the same run with
NVIDIA_DRIVER_CAPABILITIES=compute exits 0.

EVERY Track 3 unit card sets `gpu = true`, so on a gVisor worker without this, 100% of Track 3
fails to launch. And this launcher, not the hub's ingestion program, produces the ranked
events/sec number.
"""

from __future__ import annotations

import inspect
import re

from throughput import run_unit, timer


def test_gpu_args_declare_the_capability():
    assert timer.gpu_docker_args("all") == [
        "--gpus",
        "all",
        "-e",
        "NVIDIA_DRIVER_CAPABILITIES=compute",
    ]


def test_no_gpu_means_no_flags():
    """A CPU-only run must not acquire a capability variable as a side effect."""
    assert timer.gpu_docker_args(None) == []
    assert timer.gpu_docker_args("") == []


def test_the_capability_is_compute_not_utility():
    """`utility` injects the nvidia-persistenced socket, which a participant container has no
    business holding. Widening this is a security decision, so the default is pinned."""
    assert timer.DRIVER_CAPABILITIES == "compute"


def test_both_launchers_build_gpu_flags_through_the_shared_helper():
    """The real failure mode here is a partial fix.

    There are THREE independent launchers -- run_unit.py, timer.py, and the hub's ingestion
    program -- and none inherits from another. A capability added to one is a capability missing
    from the runs the other two drive. This asserts neither T3 launcher hand-rolls `--gpus` in a
    docker argv: the only place that string may be constructed is `gpu_docker_args` itself.
    """
    helper = inspect.getsource(timer.gpu_docker_args)
    for mod in (run_unit, timer):
        src = inspect.getsource(mod).replace(helper, "")  # exclude the sanctioned one
        squashed = re.sub(r"\s+", "", src)
        assert '["--gpus",' not in squashed, (
            f"{mod.__name__} builds --gpus in a docker argv outside gpu_docker_args()"
        )
