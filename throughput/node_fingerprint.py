"""Node fingerprint for Track 3 timing runs.

The T3 fairness rule (hub `docs/30-EVAL-INFRA.md` §4) is that every timed run executes on the same
dedicated, otherwise-idle, fixed-SKU box, and that the node fingerprint is recorded in the log of
every timed run: a fingerprint change invalidates cross-run comparability and forces a full re-time.
This module produces that record and compares two of them.

It is deliberately dependency-free and never raises: any field the host will not tell us is
``None``, because a fingerprint that fails to collect must not be able to break a timing run.
"""

from __future__ import annotations

import json
import platform
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# Fields that define hardware/runtime comparability. A change in any of these means two timing runs
# are not comparable. Everything else in the fingerprint is recorded for the audit trail only.
COMPARABILITY_FIELDS = (
    "cpu_model",
    "cpu_count",
    "memory_bytes",
    "gpu_name",
    "gpu_count",
)


@dataclass(frozen=True)
class NodeFingerprint:
    """Identity of the machine a timing run executed on."""

    cpu_model: str | None
    cpu_count: int | None
    memory_bytes: int | None
    kernel: str | None
    platform: str | None
    docker_version: str | None
    gpu_name: str | None
    gpu_count: int | None
    nvidia_driver: str | None
    cuda_version: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def comparable_to(self, other: NodeFingerprint) -> bool:
        """True when both runs are on the same SKU, i.e. their timings may be compared."""
        return all(getattr(self, f) == getattr(other, f) for f in COMPARABILITY_FIELDS)

    def differences(self, other: NodeFingerprint) -> dict[str, tuple[Any, Any]]:
        """The comparability fields that differ, as ``{field: (self, other)}``."""
        return {
            f: (getattr(self, f), getattr(other, f))
            for f in COMPARABILITY_FIELDS
            if getattr(self, f) != getattr(other, f)
        }


def _run(cmd: list[str]) -> str | None:
    try:
        out = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    text = out.stdout.decode("utf-8", errors="replace").strip()
    return text or None


def _cpu_model() -> str | None:
    # Linux (the eval box) exposes it in /proc/cpuinfo; macOS via sysctl, for local dry runs.
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return _run(["sysctl", "-n", "machdep.cpu.brand_string"]) or (
        platform.processor() or None
    )


def _cpu_count() -> int | None:
    import os

    return os.cpu_count()


def _memory_bytes() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024  # kB -> bytes
    except (OSError, ValueError, IndexError):
        pass
    raw = _run(["sysctl", "-n", "hw.memsize"])
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def _docker_version() -> str | None:
    return _run(["docker", "version", "--format", "{{.Server.Version}}"]) or _run(
        ["docker", "--version"]
    )


def _gpu() -> tuple[str | None, int | None, str | None, str | None]:
    """``(gpu_name, gpu_count, driver_version, cuda_version)`` via nvidia-smi, all None without one."""
    listing = _run(
        [
            "nvidia-smi",
            "--query-gpu=name,driver_version",
            "--format=csv,noheader",
        ]
    )
    name = driver = None
    count = None
    if listing:
        rows = [r.strip() for r in listing.splitlines() if r.strip()]
        count = len(rows) or None
        if rows:
            parts = [p.strip() for p in rows[0].split(",")]
            name = parts[0] or None
            driver = parts[1] if len(parts) > 1 else None
    cuda = None
    banner = _run(["nvidia-smi"])
    if banner:
        m = re.search(r"CUDA Version:\s*([0-9.]+)", banner)
        if m:
            cuda = m.group(1)
    return name, count, driver, cuda


def collect() -> NodeFingerprint:
    """Fingerprint this host. Never raises; unavailable fields are ``None``."""
    gpu_name, gpu_count, driver, cuda = _gpu()
    return NodeFingerprint(
        cpu_model=_cpu_model(),
        cpu_count=_cpu_count(),
        memory_bytes=_memory_bytes(),
        kernel=platform.release() or None,
        platform=platform.platform() or None,
        docker_version=_docker_version(),
        gpu_name=gpu_name,
        gpu_count=gpu_count,
        nvidia_driver=driver,
        cuda_version=cuda,
    )


def main(argv: list[str] | None = None) -> int:
    print(json.dumps(collect().to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
