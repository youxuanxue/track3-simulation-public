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
from typing import Any, Optional

_APPLIED = False
_LimitOrder = None
_MarketOrder = None


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


def execute_order(self, order: Any) -> Optional[Any]:
    """ABIDES ``OrderBook.execute_order`` with cheap snapshots, no book-log I/O."""
    from abides_markets.messages.orderbook import OrderExecutedMsg
    from abides_markets.orders import LimitOrder

    book = self.asks if order.side.is_bid() else self.bids
    if not book:
        return None
    if isinstance(order, LimitOrder) and not book[0].order_is_match(order):
        return None

    tag = order.tag
    if tag == "MR_preprocess_ADD" or tag == "MR_preprocess_REPLACE":
        self.owner.logEvent(tag + "_POST_ONLY", {"order_id": order.order_id})
        return None

    is_ptc_exec = False
    level0 = book[0]
    if order.quantity >= level0.peek()[0].quantity:
        matched_order, matched_meta = level0.pop()
        if matched_order.is_price_to_comply:
            is_ptc_exec = True
            if matched_meta["ptc_hidden"] is False:
                raise Exception(
                    "Should not be executing on the visible half of a price to comply order!"
                )
            assert book[1].remove_order(matched_order.order_id) is not None
            if book[1].is_empty:
                del book[1]
        if level0.is_empty:
            del book[0]
    else:
        book_order, book_meta = level0.peek()
        matched_order = cheap_clone(book_order)
        matched_order.quantity = order.quantity
        book_order.quantity -= matched_order.quantity
        if book_order.is_price_to_comply:
            is_ptc_exec = True
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
    owner.send_message(matched_order.agent_id, OrderExecutedMsg(matched_order))
    owner.send_message(order.agent_id, OrderExecutedMsg(filled_order))
    return matched_order


def enter_order(
    self,
    order: Any,
    metadata: Optional[dict] = None,
    quiet: bool = False,
) -> None:
    """ABIDES ``enter_order`` — same price-level insertion, no history / book-log."""
    from abides_markets.price_level import PriceLevel

    if order.is_price_to_comply and (
        metadata is None or metadata == {} or "ptc_hidden" not in metadata
    ):
        hidden_order = cheap_clone(order)
        visible_order = cheap_clone(order)
        hidden_order.is_hidden = True
        hidden_order.limit_price += 1 if order.side.is_bid() else -1
        hidden_meta = dict(ptc_hidden=True, ptc_other_half=visible_order)
        visible_meta = dict(ptc_hidden=False, ptc_other_half=hidden_order)
        self.enter_order(hidden_order, hidden_meta, quiet=True)
        self.enter_order(visible_order, visible_meta, quiet=quiet)
        return

    book = self.bids if order.side.is_bid() else self.asks
    md = metadata or {}
    if not book:
        book.append(PriceLevel([(order, md)]))
    elif book[-1].order_has_worse_price(order):
        book.append(PriceLevel([(order, md)]))
    else:
        for i, price_level in enumerate(book):
            if price_level.order_has_better_price(order):
                book.insert(i, PriceLevel([(order, md)]))
                break
            if price_level.order_has_equal_price(order):
                book[i].add_order(order, md)
                break


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

    executed: list[tuple[int, int]] = []
    owner = self.owner
    while True:
        stp_policy = getattr(owner, "stp_policy", None)
        if stp_policy:
            opp = self.asks if order.side.is_bid() else self.bids
            if opp and opp[0].order_is_match(order):
                resting = opp[0].peek()[0]
                if resting.agent_id == order.agent_id:
                    if stp_policy == "cancel_oldest" and self.cancel_order(
                        resting, quiet=quiet
                    ):
                        continue
                    if stp_policy != "cancel_oldest":
                        if not quiet:
                            owner.send_message(
                                order.agent_id, OrderCancelledMsg(cheap_clone(order))
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
                owner.send_message(order.agent_id, OrderAcceptedMsg(order))
            break

    if self.bids:
        b0 = self.bids[0]
        owner.logEvent(
            "BEST_BID",
            "{},{},{}".format(self.symbol, b0.price, b0.total_quantity),
        )
    if self.asks:
        a0 = self.asks[0]
        owner.logEvent(
            "BEST_ASK",
            "{},{},{}".format(self.symbol, a0.price, a0.total_quantity),
        )

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

    order = cheap_clone(order)
    owner = self.owner
    while order.quantity > 0:
        stp_policy = getattr(owner, "stp_policy", None)
        if stp_policy:
            opp = self.asks if order.side.is_bid() else self.bids
            if opp and opp[0].peek()[0].agent_id == order.agent_id:
                if stp_policy == "cancel_oldest" and self.cancel_order(opp[0].peek()[0]):
                    continue
                if stp_policy != "cancel_oldest":
                    owner.send_message(
                        order.agent_id, OrderCancelledMsg(cheap_clone(order))
                    )
                    break
        if self.execute_order(order) is None:
            break


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

    self.messages.put((deliver_at, (sender_id, recipient_id, message)))

    ledger_msgs = message.messages if type(message) is MessageBatch else (message,)
    latency_ns = deliver_at - sent_time
    parent = self._current_causal_uid
    ledger = self._msg_ledger
    for lm in ledger_msgs:
        ord_ = getattr(lm, "order", None)
        ledger.append(
            {
                "message_id": lm.message_id,
                "src_id": sender_id,
                "dst_id": recipient_id,
                "t_send_ns": sent_time,
                "t_recv_ns": deliver_at,
                "latency_ns": latency_ns,
                "msg_type": lm.type(),
                "order_id": getattr(ord_, "order_id", None),
                "causal_parent": parent,
            }
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

    while (not messages.empty()) and self.current_time and (
        self.current_time <= stop_time
    ):
        self.current_time, event = messages.get()
        sender_id, recipient_id, message = event
        self.ttl_messages += 1
        self.current_agent_additional_delay = 0

        if type(message) is WakeupMsg:
            busy_until = agent_times[recipient_id]
            if busy_until > self.current_time:
                messages.put((busy_until, (sender_id, recipient_id, message)))
                continue
            agent_times[recipient_id] = self.current_time
            self._current_causal_uid = message.message_id
            self._deliver_seq_by_key[(message.message_id, recipient_id)] = (
                self._deliver_seq
            )
            self._deliver_seq += 1
            self._msg_ledger.append(
                {
                    "message_id": message.message_id,
                    "src_id": recipient_id,
                    "dst_id": recipient_id,
                    "t_send_ns": None,
                    "t_recv_ns": self.current_time,
                    "latency_ns": 0,
                    "msg_type": "AGENT_WAKEUP",
                    "order_id": None,
                    "causal_parent": None,
                }
            )
            wakeup_result = agents[recipient_id].wakeup(self.current_time)
            agent_times[recipient_id] += (
                delays[recipient_id] + self.current_agent_additional_delay
            )
            if wakeup_result is not None:
                return {"done": False, "result": wakeup_result}
        else:
            busy_until = agent_times[recipient_id]
            if busy_until > self.current_time:
                messages.put((busy_until, (sender_id, recipient_id, message)))
                continue
            agent_times[recipient_id] = self.current_time
            batch = message.messages if type(message) is MessageBatch else (message,)
            for sub in batch:
                agent_times[recipient_id] += (
                    delays[recipient_id] + self.current_agent_additional_delay
                )
                self._current_causal_uid = sub.message_id
                self._deliver_seq_by_key[(sub.message_id, recipient_id)] = (
                    self._deliver_seq
                )
                self._deliver_seq += 1
                agents[recipient_id].receive_message(
                    self.current_time, sender_id, sub
                )

    if self.gym_agents:
        self.gym_agents[0].update_raw_state()
        return {"done": True, "result": self.gym_agents[0].get_raw_state()}
    return {"done": True, "result": None}


def apply_hotpath_patches() -> None:
    """Idempotent class-level patches. Safe to call from every simulate()."""
    global _APPLIED, _LimitOrder, _MarketOrder
    if _APPLIED:
        return

    from abides_core.kernel import Kernel
    from abides_fork.config import ScenarioLatencyModel
    from abides_markets.agents import exchange_agent as ea_mod
    from abides_markets.agents import trading_agent as ta_mod
    from abides_markets.order_book import OrderBook
    from abides_markets.orders import LimitOrder, MarketOrder

    _LimitOrder = LimitOrder
    _MarketOrder = MarketOrder

    OrderBook.execute_order = execute_order  # type: ignore[method-assign]
    OrderBook.enter_order = enter_order  # type: ignore[method-assign]
    OrderBook.handle_limit_order = handle_limit_order  # type: ignore[method-assign]
    OrderBook.handle_market_order = handle_market_order  # type: ignore[method-assign]
    Kernel.send_message = send_message  # type: ignore[method-assign]
    Kernel.runner = kernel_runner  # type: ignore[method-assign]
    ScenarioLatencyModel.get_latency = get_latency  # type: ignore[method-assign]
    ea_mod.deepcopy = _cheap_or_deepcopy
    ta_mod.deepcopy = _cheap_or_deepcopy

    _APPLIED = True
