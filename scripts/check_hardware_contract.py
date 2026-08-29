#!/usr/bin/env python3
"""Compare this host against a released C7 hardware instance. Executable, not prose.

## Why this exists

The Track 3 fairness rule is "every timed run on the same pinned, otherwise-idle instance", and
until now the only way to check it was a `python -c` one-liner in `docs/TIMING-RUNBOOK.md` plus a
runbook step telling the operator to eyeball `nvidia-smi` against a SKU name written in a document.
Both failed in the same way: the private runbook said H100 while the fleet is B200, so a compliant
operator following the instruction would have HALTED the Final scoring run on every correct box.

A SKU name in a document drifts. A **GPU UUID compared against a signed C7 instance** does not, and
that is what this script does. It is the executable form of the runbook step, so the check is run
rather than read.

## What it reports, and what it refuses to conclude

    OK           every comparable field matched a `measured` C7 value
    MISMATCH     a field disagreed -- this box is not the pinned instance, do not rank on it
    UNMEASURED   the C7 instance carries `provenance: "unmeasured"` for a field we can read
    UNKNOWN      this host cannot supply the field (no NVML, no nvidia-smi, macOS, ...)

`UNKNOWN` is never `OK`. A dev laptop with no GPU must not be able to print a passing line: the
contract's own rule is that an unmeasured field forces `rankable = false` for the queue that
instance serves, and a checker that shrugs at a missing reading reintroduces exactly the
"absent means satisfied" shape the contract exists to remove.

    python scripts/check_hardware_contract.py --instance /path/to/c7_hardware.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

OK, MISMATCH, UNMEASURED, UNKNOWN = "OK", "MISMATCH", "UNMEASURED", "UNKNOWN"

#: `nvidia-smi` query field -> the C7 `hardware` key it is compared against.
_QUERIES: tuple[tuple[str, str], ...] = (
    ("gpu_uuid", "gpu_uuid"),
    ("name", "model"),
    ("driver_version", "driver"),
)


def _nvidia_smi(fields: tuple[str, ...]) -> list[str] | None:
    """One `nvidia-smi` query, bounded. ``None`` when the tool is absent or fails."""
    try:
        proc = subprocess.run(
            ["nvidia-smi", f"--query-gpu={','.join(fields)}", "--format=csv,noheader"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    lines = [ln.strip() for ln in proc.stdout.decode().splitlines() if ln.strip()]
    if len(lines) != 1:
        # Zero devices, or more than one. Track 3 pins a single-GPU instance, so either is a
        # mismatch rather than something to average over.
        return None
    return [part.strip() for part in lines[0].split(",")]


def _provenanced(block: Any, key: str) -> tuple[str, Any]:
    """Read one `{value, provenance}` field from a C7 hardware block."""
    entry = block.get(key)
    if not isinstance(entry, dict) or "value" not in entry or "provenance" not in entry:
        return UNKNOWN, None
    if entry["provenance"] != "measured":
        return UNMEASURED, entry.get("value")
    return OK, entry["value"]


def check(instance_path: Path) -> tuple[str, list[str]]:
    """Compare this host against the instance. Returns ``(verdict, lines)``."""
    document = json.loads(instance_path.read_text(encoding="utf-8"))
    hardware = document.get("hardware", {})
    lines: list[str] = [
        f"instance_id : {document.get('instance_id')}",
        f"queue_id    : {document.get('queue_id')}",
        f"categories  : {document.get('served_categories')}",
    ]

    observed = _nvidia_smi(tuple(field for field, _ in _QUERIES))
    verdicts: list[str] = []
    for index, (field, c7_key) in enumerate(_QUERIES):
        state, expected = _provenanced(hardware, c7_key)
        if state != OK:
            lines.append(f"{field:<14} {state}: C7 {c7_key} is {state.lower()}")
            verdicts.append(state)
            continue
        if observed is None:
            lines.append(
                f"{field:<14} {UNKNOWN}: nvidia-smi did not report exactly one device on this host"
            )
            verdicts.append(UNKNOWN)
            continue
        actual = observed[index]
        if str(actual) != str(expected):
            lines.append(f"{field:<14} {MISMATCH}: host={actual!r} c7={expected!r}")
            verdicts.append(MISMATCH)
        else:
            lines.append(f"{field:<14} {OK}: {actual}")
            verdicts.append(OK)

    telemetry = document.get("telemetry", {})
    lines.append(
        f"telemetry     : interval={telemetry.get('sampling_interval_ms')}ms "
        f"min_coverage={telemetry.get('min_coverage_fraction')} "
        f"max_missed={telemetry.get('max_missed_samples')}"
    )
    if telemetry.get("sampling_interval_ms") != 50:
        lines.append("telemetry     MISMATCH: Track 3 requires a 50 ms sampling interval")
        verdicts.append(MISMATCH)
    if telemetry.get("min_coverage_fraction") != 0.95:
        lines.append("telemetry     MISMATCH: Track 3 requires coverage >= 0.95")
        verdicts.append(MISMATCH)
    if "simulator" not in (document.get("served_categories") or []):
        lines.append(
            "categories    MISMATCH: this instance does not serve the 'simulator' category, so it "
            "is not a Track 3 queue"
        )
        verdicts.append(MISMATCH)

    if MISMATCH in verdicts:
        return MISMATCH, lines
    if UNKNOWN in verdicts:
        return UNKNOWN, lines
    if UNMEASURED in verdicts:
        return UNMEASURED, lines
    return OK, lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="check_hardware_contract.py", description=__doc__)
    parser.add_argument("--instance", required=True, type=Path, help="released C7 instance JSON")
    args = parser.parse_args(argv)
    if not args.instance.exists():
        print(f"ERROR: no C7 instance at {args.instance}", file=sys.stderr)
        return 2
    verdict, lines = check(args.instance)
    for line in lines:
        print(line)
    print(f"\nVERDICT: {verdict}")
    # Only OK exits zero. UNKNOWN and UNMEASURED are non-zero on purpose: an operator script that
    # treats "we could not tell" as "fine" is the failure this replaces.
    return 0 if verdict == OK else 1


if __name__ == "__main__":
    raise SystemExit(main())
