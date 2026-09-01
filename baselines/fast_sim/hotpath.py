"""Bit-exact OrderBook + Kernel hot path (Python fallback).

Replaces ABIDES ``deepcopy`` on the matching path with a field-copy that keeps
``order_id`` (no ``Order._order_id_counter`` bump) and still allocates **two**
order objects where ABIDES does: the book copy vs the accept/fill snapshot.
In-place remaining-qty updates are what ABIDES already does on the book copy
after that snapshot; the snapshot is the semantics, not an optional clone.

The compiled twin is ``fast_sim._hotpath`` (Cython). Both expose
``apply_hotpath_patches``.
"""

from __future__ import annotations

from copy import deepcopy as _real_deepcopy
from heapq import heappop, heappush
from typing import Any, Optional


class EventQueue:
    """Python heapq twin of the C ``EventQueue``. Same ABIDES key."""

    __slots__ = ("_heap",)

    def __init__(self) -> None:
        self._heap: list = []

    def __len__(self) -> int:
        return len(self._heap)

    def empty(self) -> bool:
        return not self._heap

    def __bool__(self) -> bool:
        return bool(self._heap)

    @property
    def queue(self) -> "EventQueue":
        # Kernel.run formats len(self.messages.queue) before runner starts.
        return self

    def push(self, deliver_at: Any, sender_id: int, recipient_id: int, message: Any) -> None:
        heappush(self._heap, (deliver_at, (sender_id, recipient_id, message)))

    def pop(self) -> tuple:
        deliver_at, event = heappop(self._heap)
        sender_id, recipient_id, message = event
        return deliver_at, sender_id, recipient_id, message

    def put(self, item: Any) -> None:
        event = item[1]
        self.push(item[0], event[0], event[1], event[2])

    def get(self) -> Any:
        da, sid, rid, msg = self.pop()
        return (da, (sid, rid, msg))

_APPLIED = False
_LimitOrder = None
_MarketOrder = None
_Message = None
_OrderExecutedMsg = None
_OrderAcceptedMsg = None
_OrderCancelledMsg = None
_WakeupMsg = None
_PriceLevel = None
_BID = None
_ASK = None


def make_order_msg(cls: Any, order: Any) -> Any:
    """Same fields + ``message_id`` as the dataclass, without ``__post_init__``."""
    m = cls.__new__(cls)
    m.order = order
    mid = _Message._Message__message_id_counter
    m.message_id = mid
    _Message._Message__message_id_counter = mid + 1
    return m


def make_empty_msg(cls: Any) -> Any:
    m = cls.__new__(cls)
    mid = _Message._Message__message_id_counter
    m.message_id = mid
    _Message._Message__message_id_counter = mid + 1
    return m


def _exch_kernel_send(owner: Any, recipient_id: int, message: Any) -> None:
    owner.kernel.send_message(owner.id, recipient_id, message, delay=owner.pipeline_delay)


def cheap_clone(order: Any) -> Any:
    """Snapshot an order without touching ``Order._order_id_counter``.

    Equivalent to ``LimitOrder.__deepcopy__`` / ``MarketOrder.__deepcopy__`` for
    the fields ABIDES matching and logging read. Tags in Track-3 agents are
    ``None`` or immutable scalars; assignment matches ``deepcopy`` for those.
    """
    cls = type(order)
    clone = cls.__new__(cls)
    clone.agent_id = order.agent_id
    clone.time_placed = order.time_placed
    clone.symbol = order.symbol
    clone.quantity = order.quantity
    clone.side = order.side
    clone.order_id = order.order_id
    clone.fill_price = order.fill_price
    clone.tag = order.tag
    if cls is _LimitOrder:
        clone.limit_price = order.limit_price
        clone.is_hidden = order.is_hidden
        clone.is_price_to_comply = order.is_price_to_comply
        clone.insert_by_id = order.insert_by_id
        clone.is_post_only = order.is_post_only
    return clone


def _cheap_or_deepcopy(obj: Any, memo: Any = None) -> Any:
    t = type(obj)
    if t is _LimitOrder or t is _MarketOrder:
        return cheap_clone(obj)
    if memo is None:
        return _real_deepcopy(obj)
    return _real_deepcopy(obj, memo)


def get_latency(self, sender_id: int, recipient_id: int) -> int:
    """Same draws as ``ScenarioLatencyModel.get_latency``, without ``np.clip``."""
    if sender_id == recipient_id:
        return 0
    model = self._model
    if model == "log_normal":
        value = self.random_state.lognormal(mean=self._mu, sigma=self._sigma)
    elif model == "uniform":
        value = self.random_state.uniform(self._min_ns, self._max_ns)
    elif model == "pareto":
        base = self._min_ns if self._min_ns > 0 else 1.0
        value = base * (1.0 + self.random_state.pareto(self._alpha))
    else:
        value = self._mean_ns
    lo = self._min_ns
    hi = self._max_ns
    if value < lo:
        value = lo
    elif value > hi:
        value = hi
    return int(round(float(value)))


def _pl_peek(level: Any) -> Any:
    vis = level.visible_orders
    if vis:
        return vis[0]
    hid = level.hidden_orders
    if hid:
        return hid[0]
    raise ValueError(
        "Can't peek at LimitOrder in PriceLevel as it contains no orders"
    )


def _pl_pop(level: Any) -> Any:
    vis = level.visible_orders
    if vis:
        item = vis.pop(0)
        level._visible_qty = level._visible_qty - item[0].quantity
        return item
    hid = level.hidden_orders
    if hid:
        return hid.pop(0)
    raise ValueError(
        "Can't pop LimitOrder from PriceLevel as it contains no orders"
    )


def _pl_add(level: Any, order: Any, md: Any) -> None:
    if order.is_hidden:
        level.hidden_orders.append((order, md))
        return
    if order.insert_by_id:
        vis = level.visible_orders
        insert_index = 0
        oid = order.order_id
        for order2, _ in vis:
            if order2.order_id > oid:
                break
            insert_index += 1
        vis.insert(insert_index, (order, md))
    else:
        level.visible_orders.append((order, md))
    level._visible_qty = level._visible_qty + order.quantity


def _pl_remove(level: Any, order_id: Any) -> Any:
    vis = level.visible_orders
    for i, (book_order, _) in enumerate(vis):
        if book_order.order_id == order_id:
            item = vis.pop(i)
            level._visible_qty = level._visible_qty - item[0].quantity
            return item
    hid = level.hidden_orders
    for i, (book_order, _) in enumerate(hid):
        if book_order.order_id == order_id:
            return hid.pop(i)
    return None


def _pl_is_match(level: Any, order: Any, is_bid: bool) -> bool:
    if is_bid:
        if order.limit_price < level.price:
            return False
    elif order.limit_price > level.price:
        return False
    if order.is_post_only and level._visible_qty == 0:
        return False
    return True


def _new_level(order: Any, md: Any) -> Any:
    level = _PriceLevel.__new__(_PriceLevel)
    level.price = order.limit_price
    level.side = order.side
    if order.is_hidden:
        level.visible_orders = []
        level.hidden_orders = [(order, md)]
        level._visible_qty = 0
    else:
        level.visible_orders = [(order, md)]
        level.hidden_orders = []
        level._visible_qty = order.quantity
    return level


def pl_init(self, orders) -> None:
    n = len(orders)
    if n == 0:
        raise ValueError(
            "At least one LimitOrder must be given when initialising a PriceLevel."
        )
    order0, md0 = orders[0]
    self.price = order0.limit_price
    self.side = order0.side
    if n == 1:
        md = md0 or {}
        if order0.is_hidden:
            self.visible_orders = []
            self.hidden_orders = [(order0, md)]
            self._visible_qty = 0
        else:
            self.visible_orders = [(order0, md)]
            self.hidden_orders = []
            self._visible_qty = order0.quantity
        return
    self.visible_orders = []
    self.hidden_orders = []
    self._visible_qty = 0
    for order, metadata in orders:
        _pl_add(self, order, metadata or {})


def pl_add_order(self, order, metadata=None) -> None:
    _pl_add(self, order, metadata or {})


def pl_peek(self):
    return _pl_peek(self)


def pl_pop(self):
    return _pl_pop(self)


def pl_remove_order(self, order_id):
    return _pl_remove(self, order_id)


def pl_update_order_quantity(self, order_id, new_quantity):
    if new_quantity == 0:
        return False
    vis = self.visible_orders
    for i, (order, metadata) in enumerate(vis):
        if order.order_id == order_id:
            old = order.quantity
            if new_quantity <= old:
                order.quantity = new_quantity
            else:
                vis.pop(i)
                order.quantity = new_quantity
                vis.append((order, metadata))
            self._visible_qty = self._visible_qty + (new_quantity - old)
            return True
    hid = self.hidden_orders
    for i, (order, metadata) in enumerate(hid):
        if order.order_id == order_id:
            old = order.quantity
            if new_quantity <= old:
                order.quantity = new_quantity
            else:
                hid.pop(i)
                order.quantity = new_quantity
                hid.append((order, metadata))
            return True
    return False


def pl_order_is_match(self, order) -> bool:
    if order.side == self.side:
        raise ValueError("Attempted to compare order on wrong side of book")
    return _pl_is_match(self, order, order.side is _BID)


def pl_order_has_better_price(self, order) -> bool:
    if order.side != self.side:
        raise ValueError("Attempted to compare order on wrong side of book")
    px = order.limit_price
    lp = self.price
    if order.side is _BID:
        return px > lp
    return px < lp


def pl_order_has_worse_price(self, order) -> bool:
    if order.side != self.side:
        raise ValueError("Attempted to compare order on wrong side of book")
    px = order.limit_price
    lp = self.price
    if order.side is _BID:
        return px < lp
    return px > lp


def pl_order_has_equal_price(self, order) -> bool:
    if order.side != self.side:
        raise ValueError("Attempted to compare order on wrong side of book")
    return order.limit_price == self.price


def execute_order(self, order: Any) -> Optional[Any]:
    """ABIDES ``OrderBook.execute_order`` with cheap snapshots, no book-log I/O."""
    from abides_markets.messages.orderbook import OrderExecutedMsg

    is_bid = order.side is _BID
    book = self.asks if is_bid else self.bids
    if not book:
        return None
    level0 = book[0]
    if type(order) is _LimitOrder and not _pl_is_match(level0, order, is_bid):
        return None

    tag = order.tag
    if tag == "MR_preprocess_ADD" or tag == "MR_preprocess_REPLACE":
        self.owner.logEvent(tag + "_POST_ONLY", {"order_id": order.order_id})
        return None

    peek0 = _pl_peek(level0)
    if order.quantity >= peek0[0].quantity:
        matched_order, matched_meta = _pl_pop(level0)
        if matched_order.is_price_to_comply:
            if matched_meta["ptc_hidden"] is False:
                raise Exception(
                    "Should not be executing on the visible half of a price to comply order!"
                )
            assert _pl_remove(book[1], matched_order.order_id) is not None
            other = book[1]
            if not other.visible_orders and not other.hidden_orders:
                del book[1]
        if not level0.visible_orders and not level0.hidden_orders:
            del book[0]
    else:
        book_order, book_meta = peek0
        matched_order = cheap_clone(book_order)
        matched_order.quantity = order.quantity
        book_order.quantity -= matched_order.quantity
        level0._visible_qty = level0._visible_qty - matched_order.quantity
        if book_order.is_price_to_comply:
            if book_meta["ptc_hidden"] is False:
                raise Exception(
                    "Should not be executing on the visible half of a price to comply order!"
                )
            book_meta["ptc_other_half"].quantity -= matched_order.quantity

    matched_order.fill_price = matched_order.limit_price

    filled_order = cheap_clone(order)
    filled_order.quantity = matched_order.quantity
    filled_order.fill_price = matched_order.fill_price
    order.quantity -= filled_order.quantity

    owner = self.owner
    _exch_kernel_send(owner, matched_order.agent_id, make_order_msg(OrderExecutedMsg, matched_order))
    _exch_kernel_send(owner, order.agent_id, make_order_msg(OrderExecutedMsg, filled_order))
    return matched_order


def enter_order(
    self,
    order: Any,
    metadata: Optional[dict] = None,
    quiet: bool = False,
) -> None:
    """ABIDES ``enter_order`` — same price-level insertion, no history / book-log."""
    is_bid = order.side is _BID
    if order.is_price_to_comply and (
        metadata is None or metadata == {} or "ptc_hidden" not in metadata
    ):
        hidden_order = cheap_clone(order)
        visible_order = cheap_clone(order)
        hidden_order.is_hidden = True
        hidden_order.limit_price += 1 if is_bid else -1
        hidden_meta = dict(ptc_hidden=True, ptc_other_half=visible_order)
        visible_meta = dict(ptc_hidden=False, ptc_other_half=hidden_order)
        self.enter_order(hidden_order, hidden_meta, quiet=True)
        self.enter_order(visible_order, visible_meta, quiet=quiet)
        return

    book = self.bids if is_bid else self.asks
    md = metadata or {}
    px = order.limit_price
    if not book:
        book.append(_new_level(order, md))
        return
    lp = book[-1].price
    if (is_bid and px < lp) or ((not is_bid) and px > lp):
        book.append(_new_level(order, md))
        return
    for i, price_level in enumerate(book):
        lp = price_level.price
        if px == lp:
            _pl_add(price_level, order, md)
            return
        if (is_bid and px > lp) or ((not is_bid) and px < lp):
            book.insert(i, _new_level(order, md))
            return


def handle_limit_order(self, order: Any, quiet: bool = False) -> None:
    """ABIDES ``handle_limit_order`` including STP cancel_newest / cancel_oldest."""
    import warnings

    from abides_markets.messages.orderbook import OrderAcceptedMsg, OrderCancelledMsg

    if order.symbol != self.symbol:
        warnings.warn(
            f"{order.symbol} order discarded. Does not match OrderBook symbol: {self.symbol}"
        )
        return
    qty = order.quantity
    if (qty <= 0) or (int(qty) != qty):
        warnings.warn(
            f"{order.symbol} order discarded. Quantity ({order.quantity}) must be a positive integer."
        )
        return
    px = order.limit_price
    if (px < 0) or (int(px) != px):
        warnings.warn(
            f"{order.symbol} order discarded. Limit price ({order.limit_price}) must be a positive integer."
        )
        return

    is_bid = order.side is _BID
    executed: list[tuple[int, int]] = []
    owner = self.owner
    while True:
        stp_policy = getattr(owner, "stp_policy", None)
        if stp_policy:
            opp = self.asks if is_bid else self.bids
            if opp:
                level0 = opp[0]
                if _pl_is_match(level0, order, is_bid):
                    resting = _pl_peek(level0)[0]
                    if resting.agent_id == order.agent_id:
                        if stp_policy == "cancel_oldest" and self.cancel_order(
                            resting, quiet=quiet
                        ):
                            continue
                        if stp_policy != "cancel_oldest":
                            if not quiet:
                                _exch_kernel_send(
                                    owner,
                                    order.agent_id,
                                    make_order_msg(OrderCancelledMsg, cheap_clone(order)),
                                )
                            break

        matched_order = self.execute_order(order)
        if matched_order is not None:
            executed.append((matched_order.quantity, matched_order.fill_price))
            if order.quantity <= 0:
                break
        else:
            # Book stores a *second* copy. Accept message keeps the working
            # residual; later fills mutate only the book copy.
            self.enter_order(cheap_clone(order), quiet=quiet)
            if not quiet:
                _exch_kernel_send(
                    owner, order.agent_id, make_order_msg(OrderAcceptedMsg, order)
                )
            break

    log = owner.log
    now = owner.current_time
    if self.bids:
        b0 = self.bids[0]
        log.append((now, "BEST_BID", b0.price, b0._visible_qty))
    if self.asks:
        a0 = self.asks[0]
        log.append((now, "BEST_ASK", a0.price, a0._visible_qty))

    if executed:
        trade_qty = 0
        trade_price = 0
        for q, p in executed:
            trade_qty += q
            trade_price += p * q
        self.last_trade = int(round(trade_price / trade_qty))


def handle_market_order(self, order: Any) -> None:
    """ABIDES ``handle_market_order`` including STP."""
    import warnings

    from abides_markets.messages.orderbook import OrderCancelledMsg

    if order.symbol != self.symbol:
        warnings.warn(
            f"{order.symbol} order discarded. Does not match OrderBook symbol: {self.symbol}"
        )
        return
    qty = order.quantity
    if (qty <= 0) or (int(qty) != qty):
        warnings.warn(
            f"{order.symbol} order discarded.  Quantity ({order.quantity}) must be a positive integer."
        )
        return

    is_bid = order.side is _BID
    order = cheap_clone(order)
    owner = self.owner
    while order.quantity > 0:
        stp_policy = getattr(owner, "stp_policy", None)
        if stp_policy:
            opp = self.asks if is_bid else self.bids
            if opp and _pl_peek(opp[0])[0].agent_id == order.agent_id:
                if stp_policy == "cancel_oldest" and self.cancel_order(_pl_peek(opp[0])[0]):
                    continue
                if stp_policy != "cancel_oldest":
                    _exch_kernel_send(
                        owner,
                        order.agent_id,
                        make_order_msg(OrderCancelledMsg, cheap_clone(order)),
                    )
                    break
        if self.execute_order(order) is None:
            break


def cancel_order(
    self,
    order: Any,
    tag: Any = None,
    cancellation_metadata: Any = None,
    quiet: bool = False,
) -> bool:
    from abides_markets.messages.orderbook import OrderCancelledMsg

    is_bid = order.side is _BID
    book = self.bids if is_bid else self.asks
    if not book:
        return False
    px = order.limit_price
    oid = order.order_id
    for i, level in enumerate(book):
        if level.price != px:
            continue
        cancelled = _pl_remove(level, oid)
        if cancelled is None:
            continue
        cancelled_order, metadata = cancelled
        if not level.visible_orders and not level.hidden_orders:
            del book[i]
        if cancelled_order.is_price_to_comply:
            self.cancel_order(metadata["ptc_other_half"], quiet=True)
        if not quiet:
            self.history.append(
                dict(
                    time=self.owner.current_time,
                    type="CANCEL",
                    order_id=cancelled_order.order_id,
                    tag=tag,
                    metadata=cancellation_metadata if tag == "auctionFill" else None,
                )
            )
            _exch_kernel_send(
                self.owner,
                order.agent_id,
                make_order_msg(OrderCancelledMsg, cancelled_order),
            )
        self.last_update_ts = self.owner.current_time
        return True
    return False


def send_message(
    self,
    sender_id: int,
    recipient_id: int,
    message: Any,
    delay: int = 0,
) -> None:
    """ABIDES ``Kernel.send_message`` + Track-3 ledger, no debug formatting."""
    from abides_core.message import MessageBatch

    sent_time = (
        self.current_time
        + self.agent_computation_delays[sender_id]
        + self.current_agent_additional_delay
        + delay
    )
    latency_model = self.agent_latency_model
    if latency_model is not None:
        latency = latency_model.get_latency(
            sender_id=sender_id, recipient_id=recipient_id
        )
        deliver_at = sent_time + int(latency)
    else:
        latency = self.agent_latency[sender_id][recipient_id]
        noise = self.random_state.choice(len(self.latency_noise), p=self.latency_noise)
        deliver_at = sent_time + int(latency + noise)

    self.messages.push(deliver_at, sender_id, recipient_id, message)

    ledger_msgs = message.messages if type(message) is MessageBatch else (message,)
    latency_ns = deliver_at - sent_time
    parent = self._current_causal_uid
    ledger = self._msg_ledger
    pending = getattr(self, "_pending_ledger", None)
    for lm in ledger_msgs:
        ord_ = getattr(lm, "order", None)
        entry = (
            lm.message_id,
            sender_id,
            recipient_id,
            sent_time,
            deliver_at,
            latency_ns,
            type(lm).__name__,
            getattr(ord_, "order_id", None),
            parent,
        )
        ledger.append(entry)
        if pending is not None:
            pending[(lm.message_id, recipient_id)] = entry


def set_wakeup(self, sender_id: int, requested_time: Any = None) -> None:
    from abides_core.message import WakeupMsg

    if requested_time is None:
        requested_time = self.current_time + 1
    if self.current_time and requested_time < self.current_time:
        raise ValueError(
            "set_wakeup() called with requested time not in future",
            "current_time:",
            self.current_time,
            "requested_time:",
            requested_time,
        )
    self.messages.push(
        requested_time, sender_id, sender_id, make_empty_msg(WakeupMsg)
    )


def kernel_runner(self, agent_actions: Any = None) -> dict[str, Any]:
    """ABIDES ``Kernel.runner`` without gym / periodic ``fmt_ts`` logging."""
    from abides_core.message import MessageBatch, WakeupMsg

    if agent_actions is not None:
        exp_agent, action_list = agent_actions
        exp_agent.apply_actions(action_list)

    messages = self.messages
    agent_times = self.agent_current_times
    agents = self.agents
    delays = self.agent_computation_delays
    stop_time = self.stop_time
    pending = getattr(self, "_pending_ledger", None)
    delivered = getattr(self, "_delivered", None)

    while (
        not messages.empty()
        and self.current_time
        and (self.current_time <= stop_time)
    ):
        self.current_time, sender_id, recipient_id, message = messages.pop()
        self.ttl_messages += 1
        self.current_agent_additional_delay = 0

        if type(message) is WakeupMsg:
            busy_until = agent_times[recipient_id]
            if busy_until > self.current_time:
                messages.push(busy_until, sender_id, recipient_id, message)
                continue
            agent_times[recipient_id] = self.current_time
            self._current_causal_uid = message.message_id
            seq = self._deliver_seq
            self._deliver_seq_by_key[(message.message_id, recipient_id)] = seq
            self._deliver_seq = seq + 1
            entry = (
                message.message_id,
                recipient_id,
                recipient_id,
                None,
                self.current_time,
                0,
                "AGENT_WAKEUP",
                None,
                None,
                seq,
            )
            self._msg_ledger.append(entry)
            if delivered is not None:
                delivered.append(entry)
            wakeup_result = agents[recipient_id].wakeup(self.current_time)
            agent_times[recipient_id] += (
                delays[recipient_id] + self.current_agent_additional_delay
            )
            if wakeup_result is not None:
                return {"done": False, "result": wakeup_result}
        else:
            busy_until = agent_times[recipient_id]
            if busy_until > self.current_time:
                messages.push(busy_until, sender_id, recipient_id, message)
                continue
            agent_times[recipient_id] = self.current_time
            batch = message.messages if type(message) is MessageBatch else (message,)
            for sub in batch:
                agent_times[recipient_id] += (
                    delays[recipient_id] + self.current_agent_additional_delay
                )
                self._current_causal_uid = sub.message_id
                seq = self._deliver_seq
                self._deliver_seq_by_key[(sub.message_id, recipient_id)] = seq
                self._deliver_seq += 1
                if pending is not None:
                    entry = pending.pop((sub.message_id, recipient_id), None)
                    if entry is not None and delivered is not None:
                        delivered.append(
                            (
                                entry[0],
                                entry[1],
                                entry[2],
                                entry[3],
                                entry[4],
                                entry[5],
                                entry[6],
                                entry[7],
                                entry[8],
                                seq,
                            )
                        )
                agents[recipient_id].receive_message(
                    self.current_time, sender_id, sub
                )

    if self.gym_agents:
        self.gym_agents[0].update_raw_state()
        return {"done": True, "result": self.gym_agents[0].get_raw_state()}
    return {"done": True, "result": None}


def apply_hotpath_patches() -> None:
    """Idempotent class-level patches. Safe to call from every simulate()."""
    global _APPLIED, _LimitOrder, _MarketOrder, _Message, _PriceLevel, _BID, _ASK
    if _APPLIED:
        return

    from abides_core.kernel import Kernel
    from abides_core.message import Message
    from abides_fork.config import ScenarioLatencyModel
    from abides_markets.agents import exchange_agent as ea_mod
    from abides_markets.agents import trading_agent as ta_mod
    from abides_markets.order_book import OrderBook
    from abides_markets.orders import LimitOrder, MarketOrder, Side
    from abides_markets.price_level import PriceLevel

    _LimitOrder = LimitOrder
    _MarketOrder = MarketOrder
    _Message = Message
    _PriceLevel = PriceLevel
    _BID = Side.BID
    _ASK = Side.ASK

    OrderBook.execute_order = execute_order  # type: ignore[method-assign]
    OrderBook.enter_order = enter_order  # type: ignore[method-assign]
    OrderBook.handle_limit_order = handle_limit_order  # type: ignore[method-assign]
    OrderBook.handle_market_order = handle_market_order  # type: ignore[method-assign]
    OrderBook.cancel_order = cancel_order  # type: ignore[method-assign]
    Kernel.send_message = send_message  # type: ignore[method-assign]
    Kernel.set_wakeup = set_wakeup  # type: ignore[method-assign]
    Kernel.runner = kernel_runner  # type: ignore[method-assign]
    ScenarioLatencyModel.get_latency = get_latency  # type: ignore[method-assign]
    ea_mod.deepcopy = _cheap_or_deepcopy
    ta_mod.deepcopy = _cheap_or_deepcopy

    _APPLIED = True


def apply_book_patches() -> None:
    from abides_markets.order_book import OrderBook
    from abides_markets.price_level import PriceLevel

    PriceLevel.__init__ = pl_init  # type: ignore[method-assign]
    PriceLevel.add_order = pl_add_order  # type: ignore[method-assign]
    PriceLevel.peek = pl_peek  # type: ignore[method-assign]
    PriceLevel.pop = pl_pop  # type: ignore[method-assign]
    PriceLevel.remove_order = pl_remove_order  # type: ignore[method-assign]
    PriceLevel.update_order_quantity = pl_update_order_quantity  # type: ignore[method-assign]
    PriceLevel.order_is_match = pl_order_is_match  # type: ignore[method-assign]
    PriceLevel.order_has_better_price = pl_order_has_better_price  # type: ignore[method-assign]
    PriceLevel.order_has_worse_price = pl_order_has_worse_price  # type: ignore[method-assign]
    PriceLevel.order_has_equal_price = pl_order_has_equal_price  # type: ignore[method-assign]
    OrderBook.cancel_order = cancel_order  # type: ignore[method-assign]
