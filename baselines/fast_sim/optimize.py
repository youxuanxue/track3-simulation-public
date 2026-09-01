"""Runtime patches that do not change ABIDES matching / message / RNG order.

Every hook here is either (a) a no-op of a side channel the Track-3 adapter
never reads, or (b) a cheaper implementation of a function whose observable
return value is identical. Changing matching, latency draws, computation
delay, or message-id assignment is forbidden.
"""

from __future__ import annotations

import heapq
import logging
import sys
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

    # Compiled (Cython) OrderBook + Kernel hot path; Python fallback is bit-exact.
    try:
        from fast_sim._hotpath import apply_hotpath_patches
    except ImportError:
        from fast_sim.hotpath import apply_hotpath_patches

    apply_hotpath_patches()
    _apply_phase3_patches()

    _APPLIED = True


def _cheap_limit_str(self) -> str:
    """No ``fmt_ts`` / ``dollarize``. Debug f-strings still interpolate orders."""
    return f"LO {self.order_id} {self.side} {self.quantity}@{self.limit_price}"


def _cheap_market_str(self) -> str:
    return f"MO {self.order_id} {self.side} {self.quantity}"


def _apply_phase3_patches() -> None:
    """Kill remaining Python tax the Phase-2 profiler named, without touching fills."""
    from abides_core.agent import Agent
    from abides_markets.agents.trading_agent import TradingAgent
    from abides_markets.order_book import OrderBook
    from abides_markets.orders import LimitOrder, MarketOrder
    from abides_markets.price_level import PriceLevel

    LimitOrder.__str__ = _cheap_limit_str  # type: ignore[method-assign]
    LimitOrder.__repr__ = _cheap_limit_str  # type: ignore[method-assign]
    MarketOrder.__str__ = _cheap_market_str  # type: ignore[method-assign]
    MarketOrder.__repr__ = _cheap_market_str  # type: ignore[method-assign]

    logging.Logger.debug = _noop  # type: ignore[method-assign]

    def send_message(self, recipient_id: int, message: Any, delay: int = 0) -> None:
        self.kernel.send_message(self.id, recipient_id, message, delay=delay)

    Agent.send_message = send_message  # type: ignore[method-assign]

    _orig_add = PriceLevel.add_order
    _orig_pop = PriceLevel.pop
    _orig_remove = PriceLevel.remove_order
    _orig_update = PriceLevel.update_order_quantity

    def add_order(self, order, metadata=None):
        hidden = order.is_hidden
        qty = order.quantity
        _orig_add(self, order, metadata)
        if not hidden:
            self._visible_qty = getattr(self, "_visible_qty", 0) + qty

    def pop(self):
        order, meta = _orig_pop(self)
        if not order.is_hidden:
            self._visible_qty = getattr(self, "_visible_qty", 0) - order.quantity
        return order, meta

    def remove_order(self, order_id: int):
        result = _orig_remove(self, order_id)
        if result is not None and not result[0].is_hidden:
            self._visible_qty = getattr(self, "_visible_qty", 0) - result[0].quantity
        return result

    def update_order_quantity(self, order_id: int, new_quantity: int) -> bool:
        old = None
        hidden = False
        for o, _ in self.visible_orders:
            if o.order_id == order_id:
                old = o.quantity
                break
        else:
            for o, _ in self.hidden_orders:
                if o.order_id == order_id:
                    old = o.quantity
                    hidden = True
                    break
        ok = _orig_update(self, order_id, new_quantity)
        if ok and old is not None and not hidden:
            self._visible_qty = getattr(self, "_visible_qty", 0) + (
                new_quantity - old
            )
        return ok

    def total_quantity(self) -> int:
        q = getattr(self, "_visible_qty", None)
        if q is None:
            q = sum(o.quantity for o, _ in self.visible_orders)
            self._visible_qty = q
        return q

    PriceLevel.add_order = add_order  # type: ignore[method-assign]
    PriceLevel.pop = pop  # type: ignore[method-assign]
    PriceLevel.remove_order = remove_order  # type: ignore[method-assign]
    PriceLevel.update_order_quantity = update_order_quantity  # type: ignore[method-assign]
    PriceLevel.total_quantity = property(total_quantity)  # type: ignore[method-assign]

    def get_l2_bid_data(self, depth: int = sys.maxsize):
        out = []
        for pl in self.bids[:depth]:
            q = pl.total_quantity
            if q > 0:
                out.append((pl.price, q))
        return out

    def get_l2_ask_data(self, depth: int = sys.maxsize):
        out = []
        for pl in self.asks[:depth]:
            q = pl.total_quantity
            if q > 0:
                out.append((pl.price, q))
        return out

    OrderBook.get_l2_bid_data = get_l2_bid_data  # type: ignore[method-assign]
    OrderBook.get_l2_ask_data = get_l2_ask_data  # type: ignore[method-assign]

    def create_limit_order(
        self,
        symbol,
        quantity,
        side,
        limit_price,
        order_id=None,
        is_hidden=False,
        is_price_to_comply=False,
        insert_by_id=False,
        is_post_only=False,
        ignore_risk=True,
        tag=None,
    ):
        if quantity <= 0:
            return None
        order = LimitOrder(
            agent_id=self.id,
            time_placed=self.current_time,
            symbol=symbol,
            quantity=quantity,
            side=side,
            limit_price=limit_price,
            is_hidden=is_hidden,
            is_price_to_comply=is_price_to_comply,
            insert_by_id=insert_by_id,
            is_post_only=is_post_only,
            order_id=order_id,
            tag=tag,
        )
        if not ignore_risk:
            new_holdings = self.holdings.copy()
            q = order.quantity if order.side.is_bid() else -order.quantity
            if order.symbol in new_holdings:
                new_holdings[order.symbol] += q
            else:
                new_holdings[order.symbol] = q
            at_risk = self.mark_to_market(self.holdings) - self.holdings["CASH"]
            new_at_risk = self.mark_to_market(new_holdings) - new_holdings["CASH"]
            if (new_at_risk > at_risk) and (new_at_risk > self.starting_cash):
                return None
        return order

    def order_executed(self, order) -> None:
        if self.log_orders:
            self.logEvent("ORDER_EXECUTED", order.to_dict(), deepcopy_event=False)
        qty = order.quantity if order.side.is_bid() else -order.quantity
        sym = order.symbol
        if sym in self.holdings:
            self.holdings[sym] += qty
        else:
            self.holdings[sym] = qty
        if self.holdings[sym] == 0:
            del self.holdings[sym]
        self.holdings["CASH"] -= qty * order.fill_price
        if order.order_id in self.orders:
            o = self.orders[order.order_id]
            if order.quantity >= o.quantity:
                del self.orders[order.order_id]
            else:
                o.quantity -= order.quantity

    TradingAgent.create_limit_order = create_limit_order  # type: ignore[method-assign]
    TradingAgent.order_executed = order_executed  # type: ignore[method-assign]


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
