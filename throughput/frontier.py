"""Speed–Realism frontier (plan §3.2 SF, §5; NVIDIA "Speed–Realism Frontier").

Each admissible submission is a point ``(throughput, realism)`` where realism is derived from its
stylized-fact divergence against the reference (lower divergence = more realistic). The frontier is
the Pareto set: submissions that no other submission beats on BOTH axes (as fast or faster AND as
realistic or more). It contextualizes the leaderboard — "who is buying speed with realism" — without
re-ordering the raw events/sec ranking.

Per-submission points are track-owned and computed from data we already have (``events_per_sec`` +
the stylized-fact report). Assembling the frontier across submissions is an offline aggregation over
exported results (this module), which the platform publisher can consume.
"""

from __future__ import annotations

from dataclasses import dataclass


def realism_from_divergences(
    divergences: dict[str, float], ceilings: dict[str, float]
) -> float:
    """Aggregate the stylized-fact divergences into one *divergence* number in `[0, ∞)`, normalized
    to the calibrated ceilings: the mean of ``divergence / ceiling`` over the reported facts. 0 is a
    perfect match; ``< 1`` means every fact is within its ceiling. Lower is more realistic.

    Using ratios-to-ceiling (not raw values) keeps the four facts (KS, ACF, Hill, depth-JS) — which
    live on different scales — commensurable.
    """
    ratios = [
        float(divergences[k]) / float(ceilings[k])
        for k in divergences
        if k in ceilings and ceilings[k] > 0 and divergences[k] is not None
    ]
    return sum(ratios) / len(ratios) if ratios else 0.0


@dataclass(frozen=True)
class FrontierPoint:
    """One submission on the speed–realism plane. ``sf_divergence`` is the normalized aggregate from
    :func:`realism_from_divergences` (lower = more realistic); ``throughput`` is events/sec."""

    label: str
    throughput: float
    sf_divergence: float

    @property
    def realism(self) -> float:
        """A bounded realism score in ``(0, 1]``: ``1 / (1 + divergence)`` (1.0 = exact match)."""
        return 1.0 / (1.0 + self.sf_divergence)


def pareto_frontier(points: list[FrontierPoint]) -> list[FrontierPoint]:
    """The Pareto-optimal subset: a point is on the frontier unless some other point is at least as
    fast AND at least as realistic, and strictly better on at least one axis. Returned sorted by
    descending throughput."""
    frontier: list[FrontierPoint] = []
    for p in points:
        dominated = any(
            q is not p
            and q.throughput >= p.throughput
            and q.sf_divergence <= p.sf_divergence
            and (q.throughput > p.throughput or q.sf_divergence < p.sf_divergence)
            for q in points
        )
        if not dominated:
            frontier.append(p)
    return sorted(frontier, key=lambda p: -p.throughput)
