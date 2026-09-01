# cython: language_level=3, boundscheck=False, wraparound=True
"""Compiled OrderBook + Kernel hot path. Semantics match ``hotpath.py``."""

from copy import deepcopy as _real_deepcopy
from heapq import heappop, heappush

_APPLIED = False
_LimitOrder = None
_MarketOrder = None
_OrderExecutedMsg = None
_OrderAcceptedMsg = None
_OrderCancelledMsg = None
_PriceLevel = None
_MessageBatch = None
_WakeupMsg = None
_Message = None


def make_order_msg(cls, order):
    """Same fields + ``message_id`` as the dataclass, without ``__post_init__``."""
    m = cls.__new__(cls)
    m.order = order
    mid = _Message._Message__message_id_counter
    m.message_id = mid
    _Message._Message__message_id_counter = mid + 1
    return m


def make_empty_msg(cls):
    m = cls.__new__(cls)
    mid = _Message._Message__message_id_counter
    m.message_id = mid
    _Message._Message__message_id_counter = mid + 1
    return m


def _exch_kernel_send(owner, recipient_id, message):
    owner.kernel.send_message(owner.id, recipient_id, message, delay=owner.pipeline_delay)


def cheap_clone(order):
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


def _cheap_or_deepcopy(obj, memo=None):
    t = type(obj)
    if t is _LimitOrder or t is _MarketOrder:
        return cheap_clone(obj)
    if memo is None:
        return _real_deepcopy(obj)
    return _real_deepcopy(obj, memo)


def get_latency(self, sender_id, recipient_id):
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


def execute_order(self, order):
    book = self.asks if order.side.is_bid() else self.bids
    if not book:
        return None
    if isinstance(order, _LimitOrder) and not book[0].order_is_match(order):
        return None

    tag = order.tag
    if tag == "MR_preprocess_ADD" or tag == "MR_preprocess_REPLACE":
        self.owner.logEvent(tag + "_POST_ONLY", {"order_id": order.order_id})
        return None

    level0 = book[0]
    peek0 = level0.peek()
    if order.quantity >= peek0[0].quantity:
        matched_order, matched_meta = level0.pop()
        if matched_order.is_price_to_comply:
            if matched_meta["ptc_hidden"] == False:
                raise Exception(
                    "Should not be executing on the visible half of a price to comply order!"
                )
            assert book[1].remove_order(matched_order.order_id) is not None
            if book[1].is_empty:
                del book[1]
        if level0.is_empty:
            del book[0]
    else:
        book_order, book_meta = peek0
        matched_order = cheap_clone(book_order)
        matched_order.quantity = order.quantity
        book_order.quantity -= matched_order.quantity
        vq = getattr(level0, "_visible_qty", None)
        if vq is not None:
            level0._visible_qty = vq - matched_order.quantity
        if book_order.is_price_to_comply:
            if book_meta["ptc_hidden"] == False:
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
    _exch_kernel_send(owner, matched_order.agent_id, make_order_msg(_OrderExecutedMsg, matched_order))
    _exch_kernel_send(owner, order.agent_id, make_order_msg(_OrderExecutedMsg, filled_order))
    return matched_order


def enter_order(self, order, metadata=None, quiet=False):
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

    cdef Py_ssize_t i, n
    book = self.bids if order.side.is_bid() else self.asks
    md = metadata or {}
    if not book:
        book.append(_PriceLevel([(order, md)]))
    elif book[-1].order_has_worse_price(order):
        book.append(_PriceLevel([(order, md)]))
    else:
        n = len(book)
        for i in range(n):
            price_level = book[i]
            if price_level.order_has_better_price(order):
                book.insert(i, _PriceLevel([(order, md)]))
                break
            if price_level.order_has_equal_price(order):
                book[i].add_order(order, md)
                break


def handle_limit_order(self, order, quiet=False):
    import warnings

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

    executed = []
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
                            _exch_kernel_send(
                                owner,
                                order.agent_id,
                                make_order_msg(_OrderCancelledMsg, cheap_clone(order)),
                            )
                        break

        matched_order = self.execute_order(order)
        if matched_order is not None:
            executed.append((matched_order.quantity, matched_order.fill_price))
            if order.quantity <= 0:
                break
        else:
            self.enter_order(cheap_clone(order), quiet=quiet)
            if not quiet:
                _exch_kernel_send(
                    owner, order.agent_id, make_order_msg(_OrderAcceptedMsg, order)
                )
            break

    log = owner.log
    now = owner.current_time
    if self.bids:
        b0 = self.bids[0]
        log.append((now, "BEST_BID", b0.price, b0.total_quantity))
    if self.asks:
        a0 = self.asks[0]
        log.append((now, "BEST_ASK", a0.price, a0.total_quantity))

    if executed:
        trade_qty = 0
        trade_price = 0
        for q, p in executed:
            trade_qty += q
            trade_price += p * q
        self.last_trade = int(round(trade_price / trade_qty))


def handle_market_order(self, order):
    import warnings

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
                    _exch_kernel_send(
                        owner,
                        order.agent_id,
                        make_order_msg(_OrderCancelledMsg, cheap_clone(order)),
                    )
                    break
        if self.execute_order(order) is None:
            break


def send_message(self, sender_id, recipient_id, message, delay=0):
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

    heappush(self.messages.queue, (deliver_at, (sender_id, recipient_id, message)))

    ledger_msgs = message.messages if type(message) is _MessageBatch else (message,)
    latency_ns = deliver_at - sent_time
    parent = self._current_causal_uid
    ledger = self._msg_ledger
    pending = getattr(self, "_pending_ledger", None)
    for lm in ledger_msgs:
        ord_ = getattr(lm, "order", None)
        entry = {
            "message_id": lm.message_id,
            "src_id": sender_id,
            "dst_id": recipient_id,
            "t_send_ns": sent_time,
            "t_recv_ns": deliver_at,
            "latency_ns": latency_ns,
            "msg_type": type(lm).__name__,
            "order_id": getattr(ord_, "order_id", None),
            "causal_parent": parent,
        }
        ledger.append(entry)
        if pending is not None:
            pending[(lm.message_id, recipient_id)] = entry


def set_wakeup(self, sender_id, requested_time=None):
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
    heappush(
        self.messages.queue,
        (requested_time, (sender_id, sender_id, make_empty_msg(_WakeupMsg))),
    )


def kernel_runner(self, agent_actions=None):
    if agent_actions is not None:
        exp_agent, action_list = agent_actions
        exp_agent.apply_actions(action_list)

    q = self.messages.queue
    agent_times = self.agent_current_times
    agents = self.agents
    delays = self.agent_computation_delays
    stop_time = self.stop_time
    ledger = self._msg_ledger
    seq_by_key = self._deliver_seq_by_key
    pending = getattr(self, "_pending_ledger", None)
    delivered = getattr(self, "_delivered", None)

    while q and self.current_time and (self.current_time <= stop_time):
        self.current_time, event = heappop(q)
        sender_id, recipient_id, message = event
        self.ttl_messages += 1
        self.current_agent_additional_delay = 0

        if type(message) is _WakeupMsg:
            busy_until = agent_times[recipient_id]
            if busy_until > self.current_time:
                heappush(q, (busy_until, (sender_id, recipient_id, message)))
                continue
            agent_times[recipient_id] = self.current_time
            self._current_causal_uid = message.message_id
            seq = self._deliver_seq
            seq_by_key[(message.message_id, recipient_id)] = seq
            self._deliver_seq = seq + 1
            entry = {
                "message_id": message.message_id,
                "src_id": recipient_id,
                "dst_id": recipient_id,
                "t_send_ns": None,
                "t_recv_ns": self.current_time,
                "latency_ns": 0,
                "msg_type": "AGENT_WAKEUP",
                "order_id": None,
                "causal_parent": None,
                "seq": seq,
            }
            ledger.append(entry)
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
                heappush(q, (busy_until, (sender_id, recipient_id, message)))
                continue
            agent_times[recipient_id] = self.current_time
            batch = message.messages if type(message) is _MessageBatch else (message,)
            for sub in batch:
                agent_times[recipient_id] += (
                    delays[recipient_id] + self.current_agent_additional_delay
                )
                self._current_causal_uid = sub.message_id
                seq = self._deliver_seq
                seq_by_key[(sub.message_id, recipient_id)] = seq
                self._deliver_seq = seq + 1
                if pending is not None:
                    entry = pending.pop((sub.message_id, recipient_id), None)
                    if entry is not None:
                        entry["seq"] = seq
                        if delivered is not None:
                            delivered.append(entry)
                agents[recipient_id].receive_message(
                    self.current_time, sender_id, sub
                )

    if self.gym_agents:
        self.gym_agents[0].update_raw_state()
        return {"done": True, "result": self.gym_agents[0].get_raw_state()}
    return {"done": True, "result": None}


def apply_hotpath_patches():
    global _APPLIED, _LimitOrder, _MarketOrder
    global _OrderExecutedMsg, _OrderAcceptedMsg, _OrderCancelledMsg
    global _PriceLevel, _MessageBatch, _WakeupMsg, _Message
    if _APPLIED:
        return

    from abides_core.kernel import Kernel
    from abides_core.message import Message, MessageBatch, WakeupMsg
    from abides_fork.config import ScenarioLatencyModel
    from abides_markets.agents import exchange_agent as ea_mod
    from abides_markets.agents import trading_agent as ta_mod
    from abides_markets.messages.orderbook import (
        OrderAcceptedMsg,
        OrderCancelledMsg,
        OrderExecutedMsg,
    )
    from abides_markets.order_book import OrderBook
    from abides_markets.orders import LimitOrder, MarketOrder
    from abides_markets.price_level import PriceLevel

    _LimitOrder = LimitOrder
    _MarketOrder = MarketOrder
    _OrderExecutedMsg = OrderExecutedMsg
    _OrderAcceptedMsg = OrderAcceptedMsg
    _OrderCancelledMsg = OrderCancelledMsg
    _PriceLevel = PriceLevel
    _MessageBatch = MessageBatch
    _WakeupMsg = WakeupMsg
    _Message = Message

    OrderBook.execute_order = execute_order
    OrderBook.enter_order = enter_order
    OrderBook.handle_limit_order = handle_limit_order
    OrderBook.handle_market_order = handle_market_order
    Kernel.send_message = send_message
    Kernel.set_wakeup = set_wakeup
    Kernel.runner = kernel_runner
    ScenarioLatencyModel.get_latency = get_latency
    ea_mod.deepcopy = _cheap_or_deepcopy
    ta_mod.deepcopy = _cheap_or_deepcopy

    _APPLIED = True
