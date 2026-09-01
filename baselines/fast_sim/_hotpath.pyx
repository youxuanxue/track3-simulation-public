# cython: language_level=3, boundscheck=False, wraparound=True
"""Compiled OrderBook + Kernel hot path. Semantics match ``hotpath.py``."""

from copy import deepcopy as _real_deepcopy
from libc.stdlib cimport free, malloc, realloc
from cpython.object cimport PyObject
from cpython.ref cimport Py_DECREF, Py_INCREF, Py_XDECREF


# kind 0 = Python Message (agent-originated rare path). Other kinds are
# compact exchange/kernel hops: payload is an order or a small tuple.
KIND_PY = 0
KIND_WAKEUP = 1
KIND_EXEC = 2
KIND_ACCEPT = 3
KIND_CANCEL = 4
KIND_LIMIT = 5
KIND_CANCEL_REQ = 6
KIND_SPREAD_REQ = 7
KIND_SPREAD_RESP = 8
KIND_MKT = 9

cdef struct Event:
    long long deliver_at
    int sender_id
    int recipient_id
    long long message_id
    int kind
    PyObject *payload


cdef inline int _event_less(Event *a, Event *b) noexcept nogil:
    # Same order as (deliver_at, (sid, rid, message)) with Message.__lt__
    # by message_id. Do not collapse this to (deliver_at, message_id).
    if a.deliver_at < b.deliver_at:
        return 1
    if a.deliver_at > b.deliver_at:
        return 0
    if a.sender_id < b.sender_id:
        return 1
    if a.sender_id > b.sender_id:
        return 0
    if a.recipient_id < b.recipient_id:
        return 1
    if a.recipient_id > b.recipient_id:
        return 0
    return a.message_id < b.message_id


cdef class EventQueue:
    """C min-heap of kernel events. Comparison matches ABIDES heapq tuples."""

    cdef Event *buf
    cdef Py_ssize_t n
    cdef Py_ssize_t cap

    def __cinit__(self):
        self.n = 0
        self.cap = 64
        self.buf = <Event *>malloc(self.cap * sizeof(Event))
        if self.buf == NULL:
            raise MemoryError()

    def __dealloc__(self):
        cdef Py_ssize_t i
        if self.buf != NULL:
            for i in range(self.n):
                Py_XDECREF(self.buf[i].payload)
            free(self.buf)
            self.buf = NULL

    def __len__(self):
        return self.n

    def empty(self):
        return self.n == 0

    def __bool__(self):
        return self.n != 0

    @property
    def queue(self):
        # Kernel.run formats len(self.messages.queue) before runner starts.
        return self

    cdef int _grow(self) except -1:
        cdef Py_ssize_t newcap = self.cap * 2
        cdef Event *nb = <Event *>realloc(self.buf, newcap * sizeof(Event))
        if nb == NULL:
            raise MemoryError()
        self.buf = nb
        self.cap = newcap
        return 0

    cdef void _sift_up(self, Py_ssize_t i) noexcept nogil:
        cdef Py_ssize_t p
        cdef Event tmp
        while i > 0:
            p = (i - 1) >> 1
            if not _event_less(&self.buf[i], &self.buf[p]):
                break
            tmp = self.buf[p]
            self.buf[p] = self.buf[i]
            self.buf[i] = tmp
            i = p

    cdef void _sift_down(self, Py_ssize_t i) noexcept nogil:
        cdef Py_ssize_t l, r, smallest, n
        cdef Event tmp
        n = self.n
        while True:
            l = (i << 1) + 1
            r = l + 1
            smallest = i
            if l < n and _event_less(&self.buf[l], &self.buf[smallest]):
                smallest = l
            if r < n and _event_less(&self.buf[r], &self.buf[smallest]):
                smallest = r
            if smallest == i:
                break
            tmp = self.buf[i]
            self.buf[i] = self.buf[smallest]
            self.buf[smallest] = tmp
            i = smallest

    cdef void _push_ev(self, long long deliver_at, int sender_id, int recipient_id, long long message_id, int kind, object payload) except *:
        cdef Event ev
        cdef Py_ssize_t i
        if self.n >= self.cap:
            self._grow()
        ev.deliver_at = deliver_at
        ev.sender_id = sender_id
        ev.recipient_id = recipient_id
        ev.message_id = message_id
        ev.kind = kind
        if payload is not None:
            Py_INCREF(payload)
            ev.payload = <PyObject *>payload
        else:
            ev.payload = NULL
        i = self.n
        self.buf[i] = ev
        self.n = i + 1
        self._sift_up(i)

    cpdef void push(self, object deliver_at, int sender_id, int recipient_id, object message):
        self._push_ev(
            <long long>deliver_at,
            sender_id,
            recipient_id,
            <long long>message.message_id,
            0,
            message,
        )

    cpdef void push_event(self, object deliver_at, int sender_id, int recipient_id, object message_id, int kind, object payload):
        self._push_ev(
            <long long>deliver_at,
            sender_id,
            recipient_id,
            <long long>message_id,
            kind,
            payload,
        )

    cpdef tuple pop(self):
        if self.n == 0:
            raise IndexError("pop from empty EventQueue")
        cdef Event top = self.buf[0]
        self.n -= 1
        if self.n > 0:
            self.buf[0] = self.buf[self.n]
            self._sift_down(0)
        cdef object payload = None
        if top.payload != NULL:
            payload = <object>top.payload
            Py_DECREF(payload)
        return (
            top.deliver_at,
            top.sender_id,
            top.recipient_id,
            top.kind,
            payload,
            top.message_id,
        )

    def put(self, item):
        event = item[1]
        self.push(item[0], event[0], event[1], event[2])

    def get(self):
        da, sid, rid, kind, payload, mid = self.pop()
        return (da, (sid, rid, payload))

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
_Order = None
_LimitOrderMsg = None
_QuerySpreadMsg = None
_QuerySpreadResponseMsg = None
_CancelOrderMsg = None
_MarketOrderMsg = None
_MarketHoursRequestMsg = None
_MarketClosePriceRequestMsg = None
_BID = None
_ASK = None
_py_exch_recv_fallback = None
_py_ta_recv_fallback = None


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


def _alloc_mid():
    mid = _Message._Message__message_id_counter
    _Message._Message__message_id_counter = mid + 1
    return mid


def _ledger_send(kernel, mid, sender_id, recipient_id, sent_time, deliver_at, type_name, order_id):
    latency_ns = deliver_at - sent_time
    parent = kernel._current_causal_uid
    col = getattr(kernel, "_col_ledger", None)
    if col is not None:
        idx = col.append(
            mid, sender_id, recipient_id, sent_time, deliver_at, latency_ns,
            type_name, order_id, parent, -1,
        )
        pending = getattr(kernel, "_pending_ledger", None)
        if pending is not None:
            pending[(mid, recipient_id)] = idx
        return
    entry = (
        mid, sender_id, recipient_id, sent_time, deliver_at, latency_ns,
        type_name, order_id, parent,
    )
    kernel._msg_ledger.append(entry)
    pending = getattr(kernel, "_pending_ledger", None)
    if pending is not None:
        pending[(mid, recipient_id)] = entry


def send_compact(kernel, sender_id, recipient_id, kind, payload, delay, type_name, order_id):
    """Enqueue a compact hop and assign ``message_id`` in ABIDES construction order."""
    mid = _alloc_mid()
    sent_time = (
        kernel.current_time
        + kernel.agent_computation_delays[sender_id]
        + kernel.current_agent_additional_delay
        + delay
    )
    latency_model = kernel.agent_latency_model
    if latency_model is not None:
        latency = latency_model.get_latency(
            sender_id=sender_id, recipient_id=recipient_id
        )
        deliver_at = sent_time + int(latency)
    else:
        latency = kernel.agent_latency[sender_id][recipient_id]
        noise = kernel.random_state.choice(len(kernel.latency_noise), p=kernel.latency_noise)
        deliver_at = sent_time + int(latency + noise)
    kernel.messages.push_event(deliver_at, sender_id, recipient_id, mid, kind, payload)
    _ledger_send(kernel, mid, sender_id, recipient_id, sent_time, deliver_at, type_name, order_id)


def _exch_send_kind(owner, recipient_id, kind, order, type_name):
    send_compact(
        owner.kernel, owner.id, recipient_id, kind, order,
        owner.pipeline_delay, type_name, order.order_id,
    )


def _materialize(kind, payload, mid):
    if kind == 2:
        m = _OrderExecutedMsg.__new__(_OrderExecutedMsg)
        m.order = payload
        m.message_id = mid
        return m
    if kind == 3:
        m = _OrderAcceptedMsg.__new__(_OrderAcceptedMsg)
        m.order = payload
        m.message_id = mid
        return m
    if kind == 4:
        m = _OrderCancelledMsg.__new__(_OrderCancelledMsg)
        m.order = payload
        m.message_id = mid
        return m
    if kind == 5:
        m = _LimitOrderMsg.__new__(_LimitOrderMsg)
        m.order = payload
        m.message_id = mid
        return m
    if kind == 6:
        m = _CancelOrderMsg.__new__(_CancelOrderMsg)
        m.order = payload[0]
        m.tag = payload[1]
        m.metadata = payload[2]
        m.message_id = mid
        return m
    if kind == 7:
        m = _QuerySpreadMsg.__new__(_QuerySpreadMsg)
        m.symbol = payload[0]
        m.depth = payload[1]
        m.message_id = mid
        return m
    if kind == 8:
        m = _QuerySpreadResponseMsg.__new__(_QuerySpreadResponseMsg)
        m.symbol = payload[0]
        m.depth = payload[1]
        m.bids = payload[2]
        m.asks = payload[3]
        m.last_trade = payload[4]
        m.mkt_closed = False
        m.message_id = mid
        return m
    if kind == 9:
        m = _MarketOrderMsg.__new__(_MarketOrderMsg)
        m.order = payload
        m.message_id = mid
        return m
    return payload


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


cdef inline bint _side_is_bid(object order) except -1:
    return order.side is _BID


cdef object _pl_peek(object level):
    vis = level.visible_orders
    if vis:
        return vis[0]
    hid = level.hidden_orders
    if hid:
        return hid[0]
    raise ValueError(
        "Can't peek at LimitOrder in PriceLevel as it contains no orders"
    )


cdef object _pl_pop(object level):
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


cdef void _pl_add(object level, object order, object md) except *:
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


cdef object _pl_remove(object level, object order_id):
    cdef Py_ssize_t i, n
    vis = level.visible_orders
    n = len(vis)
    for i in range(n):
        if vis[i][0].order_id == order_id:
            item = vis.pop(i)
            level._visible_qty = level._visible_qty - item[0].quantity
            return item
    hid = level.hidden_orders
    n = len(hid)
    for i in range(n):
        if hid[i][0].order_id == order_id:
            return hid.pop(i)
    return None


cdef inline bint _pl_is_match(object level, object order, bint is_bid) except -1:
    if is_bid:
        if order.limit_price < level.price:
            return 0
    elif order.limit_price > level.price:
        return 0
    if order.is_post_only and level._visible_qty == 0:
        return 0
    return 1


cdef inline object _new_level(object order, object md):
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


def pl_init(self, orders):
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


def pl_add_order(self, order, metadata=None):
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
    cdef Py_ssize_t i, n
    vis = self.visible_orders
    n = len(vis)
    for i in range(n):
        order, metadata = vis[i]
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
    n = len(hid)
    for i in range(n):
        order, metadata = hid[i]
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


def pl_order_is_match(self, order):
    if order.side == self.side:
        raise ValueError("Attempted to compare order on wrong side of book")
    return bool(_pl_is_match(self, order, order.side is _BID))


def pl_order_has_better_price(self, order):
    if order.side != self.side:
        raise ValueError("Attempted to compare order on wrong side of book")
    px = order.limit_price
    lp = self.price
    if order.side is _BID:
        return px > lp
    return px < lp


def pl_order_has_worse_price(self, order):
    if order.side != self.side:
        raise ValueError("Attempted to compare order on wrong side of book")
    px = order.limit_price
    lp = self.price
    if order.side is _BID:
        return px < lp
    return px > lp


def pl_order_has_equal_price(self, order):
    if order.side != self.side:
        raise ValueError("Attempted to compare order on wrong side of book")
    return order.limit_price == self.price


def execute_order(self, order):
    cdef bint is_bid = _side_is_bid(order)
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
            if matched_meta["ptc_hidden"] == False:
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
    _exch_send_kind(owner, matched_order.agent_id, 2, matched_order, "OrderExecutedMsg")
    _exch_send_kind(owner, order.agent_id, 2, filled_order, "OrderExecutedMsg")
    return matched_order


def enter_order(self, order, metadata=None, quiet=False):
    cdef bint is_bid = _side_is_bid(order)
    cdef Py_ssize_t i, n
    cdef object px, lp, price_level
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
    n = len(book)
    for i in range(n):
        price_level = book[i]
        lp = price_level.price
        if px == lp:
            _pl_add(price_level, order, md)
            return
        if (is_bid and px > lp) or ((not is_bid) and px < lp):
            book.insert(i, _new_level(order, md))
            return


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

    cdef bint is_bid = _side_is_bid(order)
    executed = []
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
                                _exch_send_kind(
                                    owner, order.agent_id, 4,
                                    cheap_clone(order), "OrderCancelledMsg",
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
                _exch_send_kind(owner, order.agent_id, 3, order, "OrderAcceptedMsg")
            break

    now = owner.current_time
    tr = getattr(getattr(owner, "kernel", None), "_col_trace", None)
    if tr is not None:
        eid = owner.id
        if self.bids:
            b0 = self.bids[0]
            tr.add_quote(now, True, b0.price, b0._visible_qty, eid)
        if self.asks:
            a0 = self.asks[0]
            tr.add_quote(now, False, a0.price, a0._visible_qty, eid)
    else:
        log = owner.log
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

    cdef bint is_bid = _side_is_bid(order)
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
                    _exch_send_kind(
                        owner, order.agent_id, 4,
                        cheap_clone(order), "OrderCancelledMsg",
                    )
                    break
        if self.execute_order(order) is None:
            break


def cancel_order(self, order, tag=None, cancellation_metadata=None, quiet=False):
    cdef bint is_bid = _side_is_bid(order)
    cdef Py_ssize_t i, n
    book = self.bids if is_bid else self.asks
    if not book:
        return False
    px = order.limit_price
    oid = order.order_id
    n = len(book)
    for i in range(n):
        level = book[i]
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
            _exch_send_kind(
                self.owner, order.agent_id, 4,
                cancelled_order, "OrderCancelledMsg",
            )
        self.last_update_ts = self.owner.current_time
        return True
    return False


def send_message(self, sender_id, recipient_id, message, delay=0):
    t = type(message)
    kind = 0
    payload = message
    type_name = t.__name__
    order_id = None
    if t is _OrderExecutedMsg:
        kind, payload, type_name, order_id = 2, message.order, "OrderExecutedMsg", message.order.order_id
    elif t is _OrderAcceptedMsg:
        kind, payload, type_name, order_id = 3, message.order, "OrderAcceptedMsg", message.order.order_id
    elif t is _OrderCancelledMsg:
        kind, payload, type_name, order_id = 4, message.order, "OrderCancelledMsg", message.order.order_id
    elif t is _LimitOrderMsg:
        kind, payload, type_name, order_id = 5, message.order, "LimitOrderMsg", message.order.order_id
    elif t is _CancelOrderMsg:
        kind, payload, type_name = 6, (message.order, message.tag, message.metadata), "CancelOrderMsg"
        order_id = message.order.order_id
    elif t is _QuerySpreadMsg:
        kind, payload, type_name = 7, (message.symbol, message.depth), "QuerySpreadMsg"
    elif t is _QuerySpreadResponseMsg:
        kind, payload, type_name = 8, (
            message.symbol, message.depth, message.bids, message.asks, message.last_trade
        ), "QuerySpreadResponseMsg"
    elif t is _MarketOrderMsg:
        kind, payload, type_name, order_id = 9, message.order, "MarketOrderMsg", message.order.order_id

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

    if kind == 0:
        self.messages.push(deliver_at, sender_id, recipient_id, message)
        ledger_msgs = message.messages if type(message) is _MessageBatch else (message,)
        for lm in ledger_msgs:
            ord_ = getattr(lm, "order", None)
            _ledger_send(
                self, lm.message_id, sender_id, recipient_id, sent_time, deliver_at,
                type(lm).__name__, getattr(ord_, "order_id", None),
            )
        return

    self.messages.push_event(
        deliver_at, sender_id, recipient_id, message.message_id, kind, payload
    )
    _ledger_send(
        self, message.message_id, sender_id, recipient_id, sent_time, deliver_at,
        type_name, order_id,
    )


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
    self.messages.push_event(
        requested_time, sender_id, sender_id, _alloc_mid(), 1, None
    )


def kernel_runner(self, agent_actions=None):
    if agent_actions is not None:
        exp_agent, action_list = agent_actions
        exp_agent.apply_actions(action_list)

    messages = self.messages
    agent_times = self.agent_current_times
    agents = self.agents
    delays = self.agent_computation_delays
    stop_time = self.stop_time
    ledger = self._msg_ledger
    seq_by_key = self._deliver_seq_by_key
    pending = getattr(self, "_pending_ledger", None)
    delivered = getattr(self, "_delivered", None)

    col = getattr(self, "_col_ledger", None)
    while (
        not messages.empty()
        and self.current_time
        and (self.current_time <= stop_time)
    ):
        self.current_time, sender_id, recipient_id, kind, payload, mid = messages.pop()
        self.ttl_messages += 1
        self.current_agent_additional_delay = 0

        if kind == 1:
            busy_until = agent_times[recipient_id]
            if busy_until > self.current_time:
                messages.push_event(busy_until, sender_id, recipient_id, mid, 1, None)
                continue
            agent_times[recipient_id] = self.current_time
            self._current_causal_uid = mid
            seq = self._deliver_seq
            seq_by_key[(mid, recipient_id)] = seq
            self._deliver_seq = seq + 1
            if col is not None:
                col.append(
                    mid, recipient_id, recipient_id, None, self.current_time, 0,
                    "AGENT_WAKEUP", None, None, seq,
                )
            else:
                entry = (
                    mid, recipient_id, recipient_id, None, self.current_time, 0,
                    "AGENT_WAKEUP", None, None, seq,
                )
                ledger.append(entry)
                if delivered is not None:
                    delivered.append(entry)
            wakeup_result = agents[recipient_id].wakeup(self.current_time)
            agent_times[recipient_id] += (
                delays[recipient_id] + self.current_agent_additional_delay
            )
            if wakeup_result is not None:
                return {"done": False, "result": wakeup_result}
            continue

        busy_until = agent_times[recipient_id]
        if busy_until > self.current_time:
            if kind == 0:
                messages.push(busy_until, sender_id, recipient_id, payload)
            else:
                messages.push_event(busy_until, sender_id, recipient_id, mid, kind, payload)
            continue
        agent_times[recipient_id] = self.current_time

        if kind == 0:
            message = payload
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
                    if col is not None and entry is not None:
                        col.set_seq(entry, seq)
                    elif entry is not None and delivered is not None:
                        delivered.append(
                            (
                                entry[0], entry[1], entry[2], entry[3], entry[4],
                                entry[5], entry[6], entry[7], entry[8], seq,
                            )
                        )
                agents[recipient_id].receive_message(
                    self.current_time, sender_id, sub
                )
            continue

        agent_times[recipient_id] += (
            delays[recipient_id] + self.current_agent_additional_delay
        )
        self._current_causal_uid = mid
        seq = self._deliver_seq
        seq_by_key[(mid, recipient_id)] = seq
        self._deliver_seq = seq + 1
        if pending is not None:
            entry = pending.pop((mid, recipient_id), None)
            if col is not None and entry is not None:
                col.set_seq(entry, seq)
            elif entry is not None and delivered is not None:
                delivered.append(
                    (
                        entry[0], entry[1], entry[2], entry[3], entry[4],
                        entry[5], entry[6], entry[7], entry[8], seq,
                    )
                )
        if kind == 5 or kind == 6 or kind == 7 or kind == 9:
            exch_receive_compact(
                agents[recipient_id], self.current_time, sender_id, kind, payload, mid
            )
        else:
            agents[recipient_id].receive_message(
                self.current_time, sender_id, _materialize(kind, payload, mid)
            )

    if self.gym_agents:
        self.gym_agents[0].update_raw_state()
        return {"done": True, "result": self.gym_agents[0].get_raw_state()}
    return {"done": True, "result": None}


def _l2(book_side, depth):
    cdef Py_ssize_t i, n
    out = []
    n = len(book_side)
    if depth < n:
        n = depth
    for i in range(n):
        pl = book_side[i]
        q = pl._visible_qty
        if q > 0:
            out.append((pl.price, q))
    return out


def make_query_spread(symbol, depth):
    m = _QuerySpreadMsg.__new__(_QuerySpreadMsg)
    m.symbol = symbol
    m.depth = depth
    mid = _Message._Message__message_id_counter
    m.message_id = mid
    _Message._Message__message_id_counter = mid + 1
    return m


def make_spread_resp(symbol, depth, bids, asks, last_trade):
    m = _QuerySpreadResponseMsg.__new__(_QuerySpreadResponseMsg)
    m.symbol = symbol
    m.mkt_closed = False
    m.depth = depth
    m.bids = bids
    m.asks = asks
    m.last_trade = last_trade
    mid = _Message._Message__message_id_counter
    m.message_id = mid
    _Message._Message__message_id_counter = mid + 1
    return m


def exch_receive_message(self, current_time, sender_id, message):
    if current_time > self.mkt_close:
        return _py_exch_recv_fallback(self, current_time, sender_id, message)
    self.current_time = current_time
    self.kernel.agent_computation_delays[self.id] = self.computation_delay
    t = type(message)
    if t is _LimitOrderMsg:
        order = message.order
        book = self.order_books.get(order.symbol)
        if book is not None:
            book.handle_limit_order(cheap_clone(order))
            if self.data_subscriptions:
                self.publish_order_book_data()
        return
    if t is _QuerySpreadMsg:
        book = self.order_books.get(message.symbol)
        if book is not None:
            depth = message.depth
            self.kernel.send_message(
                self.id,
                sender_id,
                make_spread_resp(
                    message.symbol,
                    depth,
                    _l2(book.bids, depth),
                    _l2(book.asks, depth),
                    book.last_trade,
                ),
                delay=0,
            )
        return
    if t is _CancelOrderMsg:
        order = message.order
        book = self.order_books.get(order.symbol)
        if book is not None:
            book.cancel_order(cheap_clone(order), message.tag, message.metadata)
            if self.data_subscriptions:
                self.publish_order_book_data()
        return
    if t is _MarketHoursRequestMsg:
        return _py_exch_recv_fallback(self, current_time, sender_id, message)
    if t is _MarketOrderMsg:
        order = message.order
        book = self.order_books.get(order.symbol)
        if book is not None:
            book.handle_market_order(cheap_clone(order))
            if self.data_subscriptions:
                self.publish_order_book_data()
        return
    return _py_exch_recv_fallback(self, current_time, sender_id, message)


def exch_receive_compact(self, current_time, sender_id, kind, payload, mid):
    if current_time > self.mkt_close:
        return _py_exch_recv_fallback(
            self, current_time, sender_id, _materialize(kind, payload, mid)
        )
    self.current_time = current_time
    self.kernel.agent_computation_delays[self.id] = self.computation_delay
    if kind == 5:
        book = self.order_books.get(payload.symbol)
        if book is not None:
            book.handle_limit_order(cheap_clone(payload))
            if self.data_subscriptions:
                self.publish_order_book_data()
        return
    if kind == 7:
        symbol, depth = payload
        book = self.order_books.get(symbol)
        if book is not None:
            send_compact(
                self.kernel,
                self.id,
                sender_id,
                8,
                (symbol, depth, _l2(book.bids, depth), _l2(book.asks, depth), book.last_trade),
                0,
                "QuerySpreadResponseMsg",
                None,
            )
        return
    if kind == 6:
        order, tag, metadata = payload
        book = self.order_books.get(order.symbol)
        if book is not None:
            book.cancel_order(cheap_clone(order), tag, metadata)
            if self.data_subscriptions:
                self.publish_order_book_data()
        return
    if kind == 9:
        book = self.order_books.get(payload.symbol)
        if book is not None:
            book.handle_market_order(cheap_clone(payload))
            if self.data_subscriptions:
                self.publish_order_book_data()
        return


def sched_wakeup(self, current_time):
    self.current_time = current_time
    if self.first_wake:
        self.first_wake = False
        self.kernel.send_message(
            self.id,
            self.exchange_id,
            make_empty_msg(_MarketClosePriceRequestMsg),
            delay=0,
        )
    if self.mkt_open is None:
        self.kernel.send_message(
            self.id,
            self.exchange_id,
            make_empty_msg(_MarketHoursRequestMsg),
            delay=0,
        )
        return
    if not self.mkt_close or self.mkt_closed:
        return
    self.kernel.set_wakeup(self.id, current_time + self.interval_ns)
    send_compact(
        self.kernel, self.id, self.exchange_id, 7, (self.symbol, 1),
        0, "QuerySpreadMsg", None,
    )
    self.state = "AWAITING_SPREAD"


def place_limit_order(
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
        return
    if not ignore_risk:
        order = self.create_limit_order(
            symbol,
            quantity,
            side,
            limit_price,
            order_id,
            is_hidden,
            is_price_to_comply,
            insert_by_id,
            is_post_only,
            ignore_risk,
            tag,
        )
        if order is None:
            return
    else:
        order = _LimitOrder.__new__(_LimitOrder)
        order.agent_id = self.id
        order.time_placed = self.current_time
        order.symbol = symbol
        order.quantity = quantity
        order.side = side
        if order_id is None:
            order_id = _Order._order_id_counter
            _Order._order_id_counter = order_id + 1
        order.order_id = order_id
        order.fill_price = None
        order.tag = tag
        order.limit_price = limit_price
        order.is_hidden = is_hidden
        order.is_price_to_comply = is_price_to_comply
        order.insert_by_id = insert_by_id
        order.is_post_only = is_post_only
    self.orders[order.order_id] = cheap_clone(order)
    send_compact(
        self.kernel, self.id, self.exchange_id, 5, order, 0, "LimitOrderMsg", order.order_id
    )
    if self.log_orders:
        tr = getattr(self.kernel, "_col_trace", None)
        if tr is not None:
            tr.add_order(
                self.current_time,
                "ORDER_SUBMITTED",
                order.agent_id,
                "BID" if side is _BID else "ASK",
                order.limit_price,
                order.quantity,
                order.order_id,
            )
        else:
            self.log.append(
                (
                    self.current_time,
                    "ORDER_SUBMITTED",
                    order.agent_id,
                    "BID" if side is _BID else "ASK",
                    order.limit_price,
                    order.quantity,
                    order.order_id,
                )
            )


def noise_act(self):
    symbol = self.symbol
    bids = self.known_bids.get(symbol)
    asks = self.known_asks.get(symbol)
    bid = bids[0][0] if bids else None
    ask = asks[0][0] if asks else None
    rs = self.random_state
    size = int(max(1, round(rs.normal(self.order_size_mean, self.order_size_std))))
    buy = rs.randint(0, 2)
    offset = int(rs.randint(0, self.price_offset_ticks + 1))
    if buy:
        anchor = int(ask) if ask else (int(bid) if bid else self.reference_price)
        place_limit_order(self, symbol, size, _BID, anchor + offset)
    else:
        anchor = int(bid) if bid else (int(ask) if ask else self.reference_price)
        place_limit_order(self, symbol, size, _ASK, anchor - offset)


def sched_receive_message(self, current_time, sender_id, message):
    t = type(message)
    if t is _QuerySpreadResponseMsg:
        self.current_time = current_time
        if message.mkt_closed:
            self.mkt_closed = True
        symbol = message.symbol
        self.last_trade[symbol] = message.last_trade
        if self.mkt_closed:
            self.daily_close_price[symbol] = message.last_trade
        self.known_bids[symbol] = message.bids
        self.known_asks[symbol] = message.asks
        self.book = ""
        if self.state == "AWAITING_SPREAD":
            if not self.mkt_closed:
                self.act()
            self.state = "AWAITING_WAKEUP"
        return
    if t is _OrderExecutedMsg:
        self.current_time = current_time
        self.order_executed(message.order)
        return
    if t is _OrderAcceptedMsg:
        self.current_time = current_time
        self.order_accepted(message.order)
        return
    if t is _OrderCancelledMsg:
        self.current_time = current_time
        self.order_cancelled(message.order)
        return
    return _py_ta_recv_fallback(self, current_time, sender_id, message)


def bind_agent_hotpath(
    LimitOrderMsg,
    QuerySpreadMsg,
    QuerySpreadResponseMsg,
    CancelOrderMsg,
    MarketOrderMsg,
    MarketHoursRequestMsg,
    MarketClosePriceRequestMsg,
    Side,
    Order,
    exch_fallback,
    ta_fallback,
):
    global _LimitOrderMsg, _QuerySpreadMsg, _QuerySpreadResponseMsg
    global _CancelOrderMsg, _MarketOrderMsg, _MarketHoursRequestMsg
    global _MarketClosePriceRequestMsg, _BID, _ASK, _Order
    global _py_exch_recv_fallback, _py_ta_recv_fallback
    _LimitOrderMsg = LimitOrderMsg
    _QuerySpreadMsg = QuerySpreadMsg
    _QuerySpreadResponseMsg = QuerySpreadResponseMsg
    _CancelOrderMsg = CancelOrderMsg
    _MarketOrderMsg = MarketOrderMsg
    _MarketHoursRequestMsg = MarketHoursRequestMsg
    _MarketClosePriceRequestMsg = MarketClosePriceRequestMsg
    _BID = Side.BID
    _ASK = Side.ASK
    _Order = Order
    _py_exch_recv_fallback = exch_fallback
    _py_ta_recv_fallback = ta_fallback


def apply_hotpath_patches():
    global _APPLIED, _LimitOrder, _MarketOrder
    global _OrderExecutedMsg, _OrderAcceptedMsg, _OrderCancelledMsg
    global _PriceLevel, _MessageBatch, _WakeupMsg, _Message
    global _BID, _ASK
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
    from abides_markets.orders import LimitOrder, MarketOrder, Side
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
    _BID = Side.BID
    _ASK = Side.ASK

    OrderBook.execute_order = execute_order
    OrderBook.enter_order = enter_order
    OrderBook.handle_limit_order = handle_limit_order
    OrderBook.handle_market_order = handle_market_order
    OrderBook.cancel_order = cancel_order
    Kernel.send_message = send_message
    Kernel.set_wakeup = set_wakeup
    Kernel.runner = kernel_runner
    ScenarioLatencyModel.get_latency = get_latency
    ea_mod.deepcopy = _cheap_or_deepcopy
    ta_mod.deepcopy = _cheap_or_deepcopy

    _APPLIED = True


def apply_book_patches():
    """Replace PriceLevel methods after Phase 3 wrappers so qty-cache is not doubled."""
    from abides_markets.order_book import OrderBook
    from abides_markets.price_level import PriceLevel

    PriceLevel.__init__ = pl_init
    PriceLevel.add_order = pl_add_order
    PriceLevel.peek = pl_peek
    PriceLevel.pop = pl_pop
    PriceLevel.remove_order = pl_remove_order
    PriceLevel.update_order_quantity = pl_update_order_quantity
    PriceLevel.order_is_match = pl_order_is_match
    PriceLevel.order_has_better_price = pl_order_has_better_price
    PriceLevel.order_has_worse_price = pl_order_has_worse_price
    PriceLevel.order_has_equal_price = pl_order_has_equal_price
    OrderBook.cancel_order = cancel_order
