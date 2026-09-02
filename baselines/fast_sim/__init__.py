"""Track 3 participant simulator: ABIDES-faithful matching, faster I/O and kernel path.

The matching engine, agents, oracle, latency model and discrete-event order are
the pinned ABIDES stack (via ``abides_fork``). Speed comes from stripping work
that does not appear in ``trace.parquet`` / ``message_trace.parquet`` — locked
``PriorityQueue``, coloredlogs, book-log numpy snapshots, unused agent log
rows, pandas flattening of the full log, and serial batch runs.
"""

from __future__ import annotations

__all__ = ["simulate", "simulate_batch"]


def simulate(*args, **kwargs):
    from fast_sim.simulate import simulate as _simulate

    return _simulate(*args, **kwargs)


def simulate_batch(*args, **kwargs):
    from fast_sim.simulate_batch import simulate_batch as _simulate_batch

    return _simulate_batch(*args, **kwargs)
