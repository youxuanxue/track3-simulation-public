"""Track 3's contribution to the C3 sanitation policy: the exact output allowlist and the bounds.

C3 says the participant's output directory and the scoring program's input directory are not the
same directory, and that each track contributes the exact set of relative paths its submissions may
write. This module is Track 3's contribution, in one place, so the production Runner, the local
developer harness and the tests all bound the same set.

Track 3 outputs, and nothing else:

| Unit shape | Allowed relative paths |
|---|---|
| single-market | ``trace.parquet``, ``events.json``, ``message_trace.parquet`` |
| batch | ``batch_events.json`` plus, per declared sub, ``<sub>/{trace,message_trace}.parquet`` and ``<sub>/events.json`` |

Maximum depth 2. The sub list comes from the ORGANIZER's ``batch.json``, never from a directory
listing of what the submission produced — otherwise a submission could widen its own allowlist by
creating directories.

**Why a row bound and not only a byte bound.** Track 3's ranked numerator is the emitted trace's
row count. A padded trace is cheap in bytes (a repeated event compresses extremely well in parquet)
and expensive in rank, so a byte cap is not the bound that matters here. `max_rows_for` expresses
the row bound relative to the organizer's deterministic reference count, enforceable from the
parquet footer before any parser runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence

__all__ = [
    "BATCH_ROOT_FILES",
    "MAX_DEPTH",
    "SINGLE_UNIT_FILES",
    "SUB_FILES",
    "allowed_paths_for",
    "max_rows_for",
]

SINGLE_UNIT_FILES: tuple[str, ...] = (
    "trace.parquet",
    "events.json",
    "message_trace.parquet",
)
BATCH_ROOT_FILES: tuple[str, ...] = ("batch_events.json",)
SUB_FILES: tuple[str, ...] = ("trace.parquet", "events.json", "message_trace.parquet")
MAX_DEPTH = 2


def allowed_paths_for(unit_dir: str | Path) -> tuple[str, ...]:
    """The exact relative paths a submission for this unit may write.

    Reads ``batch.json`` from the organizer's unit directory to learn the sub list. A unit with no
    ``batch.json`` is single-market.
    """
    path = Path(unit_dir) / "batch.json"
    if not path.exists():
        return SINGLE_UNIT_FILES
    document = json.loads(path.read_text(encoding="utf-8"))
    subs: Sequence[dict[str, object]] = document["subs"]
    paths: list[str] = list(BATCH_ROOT_FILES)
    for entry in subs:
        sub = str(entry["sub"])
        paths.extend(f"{sub}/{name}" for name in SUB_FILES)
    return tuple(paths)


def max_rows_for(reference_rows: int, *, slack: float = 0.0) -> int:
    """The largest trace row count admissible for a unit whose reference has ``reference_rows``.

    ``slack`` is zero by design: a Track 3 scenario is deterministic given its seed, so a faithful
    run emits exactly as many rows as the reference. The parameter exists so a future family with a
    documented tolerance can express it rather than reaching for an inequality somewhere else.
    """
    if reference_rows < 0:
        raise ValueError("reference_rows must be non-negative")
    return int(reference_rows * (1.0 + slack))
