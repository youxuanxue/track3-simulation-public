"""abides_fork — Track 3 wrapper over pinned upstream ABIDES.

Provides the ``simulate`` CLI (the Track 3 submission verb), an agent registry
that maps scenario ``agent_type`` names to ABIDES agent classes, and the trace
extractor that turns an ABIDES ``end_state`` into the canonical 7-column
``trace.parquet`` (see ``templates/trace_column_registry.json``).

This is a thin overlay on upstream ``abides-core`` + ``abides-markets`` pinned at
a fixed commit; it applies the small compatibility patch needed to run on modern
Python and exposes the Track 3 I/O contract. It is distributed via the pre-built
baseline Docker image; there is no separate ABIDES fork repository.
"""

from __future__ import annotations

__all__ = ["extract_trace", "TRACE_COLUMNS"]


def __getattr__(name: str):
    """Load the trace adapter only for callers that request its public API.

    The ``simulate`` CLI imports the stdlib-only scenario resolver before it
    chooses the native path.  Eagerly importing the ABIDES trace adapter here
    pulled pandas and the full ABIDES package into every small scenario's cold
    start, even though native output conversion does not use that adapter.
    """
    if name in __all__:
        from abides_fork.trace import TRACE_COLUMNS, extract_trace

        return {"TRACE_COLUMNS": TRACE_COLUMNS, "extract_trace": extract_trace}[name]
    raise AttributeError(name)
