"""Runtime patches that do not change ABIDES matching / message / RNG order.

Every hook here is either (a) a no-op of a side channel the Track-3 adapter
never reads, or (b) a cheaper implementation of a function whose observable
return value is identical. Changing matching, latency draws, computation
delay, or message-id assignment is forbidden.
"""

from __future__ import annotations

import heapq
import logging
from typing import Any

# Event types that become rows in trace.parquet. Everything else (HOLDINGS_UPDATED,
# BID_DEPTH, AGENT_TYPE, STARTING_CASH, …) is allocated and later discarded by
# parse_logs_df — skip it at the source.
_TRACE_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "ORDER_SUBMITTED",
        "ORDER_ACCEPTED",
        "ORDER_EXECUTED",
        "ORDER_CANCELLED",
        "ORDER_REPLACED",
        "BEST_BID",
        "BEST_ASK",
    }
)

_APPLIED = False


def _cheap_limit_to_dict(self) -> dict[str, Any]:
    """Same keys extract_trace reads; no deepcopy, no fmt_ts."""
    return {
        "agent_id": self.agent_id,
        "order_id": self.order_id,
        "side": self.side,
        "limit_price": self.limit_price,
        "quantity": self.quantity,
        "fill_price": self.fill_price,
    }


def _cheap_market_to_dict(self) -> dict[str, Any]:
    return {
        "agent_id": self.agent_id,
        "order_id": self.order_id,
        "side": self.side,
        "quantity": self.quantity,
        "fill_price": self.fill_price,
    }


def _filtered_log_event(
    self,
    event_type: str,
    event: Any = "",
    append_summary_log: bool = False,
    deepcopy_event: bool = True,
) -> None:
    """Keep only events that feed the canonical 7-column trace."""
    if event_type not in _TRACE_EVENT_TYPES:
        return
    self.log.append((self.current_time, event_type, event))


def _noop(*_args: Any, **_kwargs: Any) -> None:
    return None


class HeapPQueue:
    """``queue.PriorityQueue`` without the threading lock.

    ABIDES stores ``(deliver_at, (sender_id, recipient_id, message))``. Message
    tie-breaks on ``message_id`` via ``Message.__lt__``. heapq uses the same
    comparison, so delivery order is unchanged.
    """

    __slots__ = ("queue",)

    def __init__(self) -> None:
        self.queue: list = []

    def put(self, item: Any) -> None:
        heapq.heappush(self.queue, item)

    def get(self) -> Any:
        return heapq.heappop(self.queue)

    def empty(self) -> bool:
        return not self.queue


class _NoAppend(list):
    """List that ignores ``append`` (order-stream history / unused txn journals)."""

    def append(self, *_args: Any, **_kwargs: Any) -> None:  # type: ignore[override]
        return None


def apply_runtime_patches() -> None:
    """Idempotent. Safe to call from every simulate() invocation."""
    global _APPLIED
    if _APPLIED:
        return

    logging.disable(logging.WARNING)
    import warnings

    warnings.filterwarnings(
        "ignore",
        message="Execution received for order not in orders list",
    )

    from abides_core.agent import Agent
    from abides_core.kernel import Kernel
    from abides_markets.agents.exchange_agent import ExchangeAgent
    from abides_markets.order_book import OrderBook
    from abides_markets.orders import LimitOrder, MarketOrder

    LimitOrder.to_dict = _cheap_limit_to_dict  # type: ignore[method-assign]
    MarketOrder.to_dict = _cheap_market_to_dict  # type: ignore[method-assign]
    Agent.logEvent = _filtered_log_event  # type: ignore[method-assign]

    # Book-log snapshots never enter trace.parquet. BEST_BID / BEST_ASK still
    # come from owner.logEvent inside handle_limit_order.
    OrderBook.append_book_log2 = _noop  # type: ignore[method-assign]

    # End-of-day book analysis / fundamental archival / summary pickle are not
    # scored outputs. write_summary_log otherwise mkdirs ./log and pickles.
    ExchangeAgent.kernel_terminating = _noop  # type: ignore[method-assign]
    ExchangeAgent.analyse_order_book = _noop  # type: ignore[method-assign]
    Kernel.write_summary_log = _noop  # type: ignore[method-assign]

    _APPLIED = True


def slim_exchange(exchange: Any) -> None:
    """Turn off exchange side-channels after ``build_config`` constructs it."""
    exchange.book_logging = False
    exchange.book_log_depth = 1
    exchange.stream_history = 0
    exchange.log_orders = False
    exchange.log_to_file = False
    books = getattr(exchange, "order_books", None) or {}
    for book in books.values():
        # Track-3 agents never send QueryOrderStream / QueryTransactedVol.
        book.history = _NoAppend()
        book.buy_transactions = _NoAppend()
        book.sell_transactions = _NoAppend()
        book.book_log2 = _NoAppend()


def slim_agents(agents: list[Any]) -> None:
    for agent in agents:
        agent.log_to_file = False
