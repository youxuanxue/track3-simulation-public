# cython: language_level=3, boundscheck=False, wraparound=True
"""Compiled OrderBook + Kernel hot path. Semantics match ``hotpath.py``."""

from copy import deepcopy as _real_deepcopy
from libc.math cimport exp, log, sqrt
from libc.stdint cimport uint32_t
from libc.stdlib cimport free, malloc, realloc
from libc.string cimport memcpy, memmove
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

# numpy.random.RandomState (legacy MT19937 / randomkit). Matches numpy 1.26
# mtrand: next32 tempering, next_double = two 32-bit rk_double, polar
# legacy_gauss with cache, masked randint via next32 when range fits 32-bit.
cdef enum:
    _MT_N = 624
    _MT_M = 397
    _MT_MATRIX_A = 0x9908b0df
    _MT_UPPER = 0x80000000
    _MT_LOWER = 0x7fffffff


cdef class MT19937:
    """Bit-exact stand-in for ``numpy.random.RandomState`` draws we use."""

    cdef uint32_t key[624]
    cdef int pos
    cdef int has_gauss
    cdef double gauss

    def bind_numpy(self, rs):
        st = rs.get_state()
        if st[0] != "MT19937":
            raise ValueError("expected RandomState MT19937")
        arr = st[1]
        cdef Py_ssize_t i
        for i in range(624):
            self.key[i] = <uint32_t>arr[i]
        self.pos = int(st[2])
        self.has_gauss = int(st[3])
        self.gauss = float(st[4])
        return self

    cdef inline void _twist(self) noexcept nogil:
        cdef int i
        cdef uint32_t y
        for i in range(_MT_N - _MT_M):
            y = (self.key[i] & _MT_UPPER) | (self.key[i + 1] & _MT_LOWER)
            self.key[i] = self.key[i + _MT_M] ^ (y >> 1) ^ ((-(y & 1)) & _MT_MATRIX_A)
        for i in range(_MT_N - _MT_M, _MT_N - 1):
            y = (self.key[i] & _MT_UPPER) | (self.key[i + 1] & _MT_LOWER)
            self.key[i] = self.key[i + (_MT_M - _MT_N)] ^ (y >> 1) ^ (
                (-(y & 1)) & _MT_MATRIX_A
            )
        y = (self.key[_MT_N - 1] & _MT_UPPER) | (self.key[0] & _MT_LOWER)
        self.key[_MT_N - 1] = self.key[_MT_M - 1] ^ (y >> 1) ^ (
            (-(y & 1)) & _MT_MATRIX_A
        )
        self.pos = 0

    cdef inline uint32_t next32(self) noexcept nogil:
        cdef uint32_t y
        if self.pos == _MT_N:
            self._twist()
        y = self.key[self.pos]
        self.pos += 1
        y ^= (y >> 11)
        y ^= (y << 7) & <uint32_t>0x9d2c5680
        y ^= (y << 15) & <uint32_t>0xefc60000
        y ^= (y >> 18)
        return y

    cdef inline double next_double(self) noexcept nogil:
        cdef uint32_t a = self.next32() >> 5
        cdef uint32_t b = self.next32() >> 6
        return (a * 67108864.0 + b) / 9007199254740992.0

    cdef inline double gauss_polar(self) noexcept nogil:
        cdef double tmp, x1, x2, r2, f
        if self.has_gauss:
            tmp = self.gauss
            self.has_gauss = 0
            self.gauss = 0.0
            return tmp
        while True:
            x1 = 2.0 * self.next_double() - 1.0
            x2 = 2.0 * self.next_double() - 1.0
            r2 = x1 * x1 + x2 * x2
            if r2 > 0.0 and r2 < 1.0:
                break
        f = sqrt(-2.0 * log(r2) / r2)
        self.gauss = f * x1
        self.has_gauss = 1
        return f * x2

    cdef inline double normal(self, double loc, double scale) noexcept nogil:
        return loc + scale * self.gauss_polar()

    cdef inline double lognormal(self, double mean, double sigma) noexcept nogil:
        # numpy.random.RandomState.lognormal == exp(normal(mean, sigma))
        return exp(self.normal(mean, sigma))

    cdef inline long randint(self, long low, long high) noexcept nogil:
        cdef uint32_t rng, mask, val
        rng = <uint32_t>(high - low - 1)
        if rng == 0:
            return low
        mask = rng
        mask |= mask >> 1
        mask |= mask >> 2
        mask |= mask >> 4
        mask |= mask >> 8
        mask |= mask >> 16
        while True:
            val = self.next32() & mask
            if val <= rng:
                return low + <long>val

    def py_normal(self, loc, scale):
        return self.normal(loc, scale)

    def py_lognormal(self, mean, sigma):
        return self.lognormal(mean, sigma)

    def py_randint(self, low, high):
        return self.randint(low, high)


cdef inline MT19937 _agent_mt(object agent):
    cdef MT19937 mt
    obj = getattr(agent, "_mt19937", None)
    if obj is None:
        mt = MT19937()
        mt.bind_numpy(agent.random_state)
        agent._mt19937 = mt
        return mt
    return <MT19937>obj


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


# --- C order / C price level / C trace+ledger (step 9) ---

cdef enum:
    EV_SUBMIT = 0
    EV_ACCEPT = 1
    EV_EXEC = 2
    EV_CANCEL = 3
    EV_REPLACE = 4
    FL_HIDDEN = 1
    FL_PTC = 2
    FL_INSERT_ID = 4
    FL_POST_ONLY = 8
    LF_SEND_NA = 1
    LF_OID_NA = 2
    LF_PARENT_NA = 4

cdef tuple _MT_NAMES = (
    "AGENT_WAKEUP",
    "OrderExecutedMsg",
    "OrderAcceptedMsg",
    "OrderCancelledMsg",
    "LimitOrderMsg",
    "CancelOrderMsg",
    "QuerySpreadMsg",
    "QuerySpreadResponseMsg",
    "MarketOrderMsg",
    "MarketHoursRequestMsg",
    "MarketClosePriceRequestMsg",
    "MarketHoursMsg",
    "MarketClosePriceMsg",
    "MarketClosedMsg",
)

cdef tuple _EV_NAMES = (
    "ORDER_FILLED",
    "ORDER_ACCEPTED",
    "ORDER_EXECUTED",
    "ORDER_CANCELLED",
    "ORDER_REPLACED",
)


cdef inline int _mtype_code(object name) noexcept:
    if name == "OrderExecutedMsg":
        return 1
    if name == "LimitOrderMsg":
        return 4
    if name == "AGENT_WAKEUP":
        return 0
    if name == "OrderAcceptedMsg":
        return 2
    if name == "QuerySpreadMsg":
        return 6
    if name == "QuerySpreadResponseMsg":
        return 7
    if name == "CancelOrderMsg":
        return 5
    if name == "OrderCancelledMsg":
        return 3
    if name == "MarketOrderMsg":
        return 8
    if name == "MarketHoursRequestMsg":
        return 9
    if name == "MarketClosePriceRequestMsg":
        return 10
    if name == "MarketHoursMsg":
        return 11
    if name == "MarketClosePriceMsg":
        return 12
    if name == "MarketClosedMsg":
        return 13
    return 0


cdef struct COrder:
    long long time_placed
    long long order_id
    int agent_id
    int quantity
    int limit_price
    int fill_price
    unsigned char side
    unsigned char flags


cdef struct LRow:
    long long mid
    long long t_send
    long long t_recv
    long long lat
    long long oid
    long long parent
    int src
    int dst
    int mtype
    int seq
    unsigned char flags


cdef class CTrace:
    """Order + quote columns in C arrays. pandas only in to_dataframe()."""

    cdef long long *ot
    cdef int *oev
    cdef int *oaid
    cdef unsigned char *oside
    cdef int *opx
    cdef int *osz
    cdef int *ooid
    cdef Py_ssize_t n_o, cap_o
    cdef long long *qt
    cdef int *qaid
    cdef unsigned char *qside
    cdef int *qpx
    cdef int *qsz
    cdef Py_ssize_t n_q, cap_q

    def __cinit__(self):
        self.ot = NULL
        self.oev = NULL
        self.oaid = NULL
        self.oside = NULL
        self.opx = NULL
        self.osz = NULL
        self.ooid = NULL
        self.qt = NULL
        self.qaid = NULL
        self.qside = NULL
        self.qpx = NULL
        self.qsz = NULL
        self.n_o = 0
        self.cap_o = 0
        self.n_q = 0
        self.cap_q = 0

    def __dealloc__(self):
        if self.ot != NULL:
            free(self.ot)
        if self.oev != NULL:
            free(self.oev)
        if self.oaid != NULL:
            free(self.oaid)
        if self.oside != NULL:
            free(self.oside)
        if self.opx != NULL:
            free(self.opx)
        if self.osz != NULL:
            free(self.osz)
        if self.ooid != NULL:
            free(self.ooid)
        if self.qt != NULL:
            free(self.qt)
        if self.qaid != NULL:
            free(self.qaid)
        if self.qside != NULL:
            free(self.qside)
        if self.qpx != NULL:
            free(self.qpx)
        if self.qsz != NULL:
            free(self.qsz)

    def __bool__(self):
        return self.n_o != 0 or self.n_q != 0

    cdef void _grow_o(self) except *:
        cdef Py_ssize_t newcap = 64 if self.cap_o == 0 else self.cap_o * 2
        cdef long long *ot = <long long *>realloc(self.ot, newcap * sizeof(long long))
        cdef int *oev = <int *>realloc(self.oev, newcap * sizeof(int))
        cdef int *oaid = <int *>realloc(self.oaid, newcap * sizeof(int))
        cdef unsigned char *oside = <unsigned char *>realloc(
            self.oside, newcap * sizeof(unsigned char)
        )
        cdef int *opx = <int *>realloc(self.opx, newcap * sizeof(int))
        cdef int *osz = <int *>realloc(self.osz, newcap * sizeof(int))
        cdef int *ooid = <int *>realloc(self.ooid, newcap * sizeof(int))
        if (
            ot == NULL or oev == NULL or oaid == NULL or oside == NULL
            or opx == NULL or osz == NULL or ooid == NULL
        ):
            raise MemoryError()
        self.ot = ot
        self.oev = oev
        self.oaid = oaid
        self.oside = oside
        self.opx = opx
        self.osz = osz
        self.ooid = ooid
        self.cap_o = newcap

    cdef void _grow_q(self) except *:
        cdef Py_ssize_t newcap = 64 if self.cap_q == 0 else self.cap_q * 2
        cdef long long *qt = <long long *>realloc(self.qt, newcap * sizeof(long long))
        cdef int *qaid = <int *>realloc(self.qaid, newcap * sizeof(int))
        cdef unsigned char *qside = <unsigned char *>realloc(
            self.qside, newcap * sizeof(unsigned char)
        )
        cdef int *qpx = <int *>realloc(self.qpx, newcap * sizeof(int))
        cdef int *qsz = <int *>realloc(self.qsz, newcap * sizeof(int))
        if qt == NULL or qaid == NULL or qside == NULL or qpx == NULL or qsz == NULL:
            raise MemoryError()
        self.qt = qt
        self.qaid = qaid
        self.qside = qside
        self.qpx = qpx
        self.qsz = qsz
        self.cap_q = newcap

    cdef void add_order_c(self, long long t, int ev, int aid, unsigned char is_bid, int px, int sz, int oid) except *:
        cdef Py_ssize_t i
        if self.n_o >= self.cap_o:
            self._grow_o()
        i = self.n_o
        self.ot[i] = t
        self.oev[i] = ev
        self.oaid[i] = aid
        self.oside[i] = is_bid
        self.opx[i] = px
        self.osz[i] = sz
        self.ooid[i] = oid
        self.n_o = i + 1

    cdef void add_quote_c(self, long long t, unsigned char is_bid, int px, int sz, int aid) except *:
        cdef Py_ssize_t i
        if self.n_q >= self.cap_q:
            self._grow_q()
        i = self.n_q
        self.qt[i] = t
        self.qaid[i] = aid
        self.qside[i] = is_bid
        self.qpx[i] = px
        self.qsz[i] = sz
        self.n_q = i + 1

    def add_order(self, t_ns, event_type, agent_id, side, price, size, order_id):
        cdef int ev
        if event_type == "ORDER_SUBMITTED":
            ev = EV_SUBMIT
        elif event_type == "ORDER_ACCEPTED":
            ev = EV_ACCEPT
        elif event_type == "ORDER_EXECUTED":
            ev = EV_EXEC
        elif event_type == "ORDER_CANCELLED":
            ev = EV_CANCEL
        else:
            ev = EV_REPLACE
        cdef unsigned char is_bid = 1 if (side is _BID or side == "BID") else 0
        self.add_order_c(t_ns, ev, agent_id, is_bid, price, size, order_id)

    def add_quote(self, t_ns, is_bid, price, size, agent_id):
        self.add_quote_c(t_ns, 1 if is_bid else 0, price, size, agent_id)

    def to_dataframe(self):
        from fast_sim.extract import _empty_trace, _stable_lexsort
        from abides_fork.trace import _TRACE_DTYPES
        import numpy as np
        import pandas as pd

        cdef Py_ssize_t i, n_order, n_quote, n, j
        n_order = self.n_o
        n_quote = self.n_q
        if n_order == 0 and n_quote == 0:
            return _empty_trace()

        if n_order:
            t_arr = np.empty(n_order, dtype=np.int64)
            aid_arr = np.empty(n_order, dtype=np.int64)
            oid_arr = np.empty(n_order, dtype=np.int64)
            px_arr = np.empty(n_order, dtype=np.int64)
            sz_arr = np.empty(n_order, dtype=np.int64)
            ev_arr = np.empty(n_order, dtype=np.int32)
            side_b = np.empty(n_order, dtype=np.uint8)
            for i in range(n_order):
                t_arr[i] = self.ot[i]
                aid_arr[i] = self.oaid[i]
                oid_arr[i] = self.ooid[i]
                px_arr[i] = self.opx[i]
                sz_arr[i] = self.osz[i]
                ev_arr[i] = self.oev[i]
                side_b[i] = self.oside[i]
            order_idx = np.argsort(t_arr, kind="stable")
            t_arr = t_arr[order_idx]
            aid_arr = aid_arr[order_idx]
            oid_arr = oid_arr[order_idx]
            px_arr = px_arr[order_idx]
            sz_arr = sz_arr[order_idx]
            ev_arr = ev_arr[order_idx]
            side_b = side_b[order_idx]
            is_exec = ev_arr == EV_EXEC
            last_exec = {}
            for pos in np.nonzero(is_exec)[0]:
                last_exec[int(oid_arr[pos])] = int(pos)
            msg = np.empty(n_order, dtype=object)
            side_str = np.empty(n_order, dtype=object)
            ev_py = ev_arr
            oid_py = oid_arr
            sb_py = side_b
            for i in range(n_order):
                ev = int(ev_py[i])
                if ev == EV_EXEC:
                    msg[i] = (
                        "ORDER_FILLED"
                        if last_exec.get(int(oid_py[i])) == i
                        else "PARTIAL_FILL"
                    )
                elif ev == EV_SUBMIT:
                    msg[i] = "ORDER_SUBMITTED"
                elif ev == EV_ACCEPT:
                    msg[i] = "ORDER_ACCEPTED"
                elif ev == EV_CANCEL:
                    msg[i] = "ORDER_CANCELLED"
                else:
                    msg[i] = "ORDER_REPLACED"
                side_str[i] = "BID" if sb_py[i] else "ASK"
        else:
            t_arr = aid_arr = oid_arr = px_arr = sz_arr = msg = side_str = None

        if n_quote:
            last_i = {}
            first_rank = {}
            rank = 0
            for i in range(n_quote):
                key = (self.qt[i], self.qside[i])
                if key not in first_rank:
                    first_rank[key] = rank
                    rank += 1
                last_i[key] = i
            kept = sorted(last_i.items(), key=lambda kv: first_rank[kv[0]])
            n_quote = len(kept)
            q_t = np.empty(n_quote, dtype=np.int64)
            q_aid = np.empty(n_quote, dtype=np.int64)
            q_side = np.empty(n_quote, dtype=object)
            q_px = np.empty(n_quote, dtype=np.int64)
            q_sz = np.empty(n_quote, dtype=np.int64)
            q_oid = np.full(n_quote, -1, dtype=np.int64)
            q_msg = np.empty(n_quote, dtype=object)
            for j, (_, i) in enumerate(kept):
                q_t[j] = self.qt[i]
                q_aid[j] = self.qaid[i]
                q_side[j] = "BID" if self.qside[i] else "ASK"
                q_px[j] = self.qpx[i]
                q_sz[j] = self.qsz[i]
                q_msg[j] = "QUOTE_UPDATE"
        else:
            n_quote = 0

        n = n_order + n_quote
        t_all = np.empty(n, dtype=np.int64)
        aid_all = np.empty(n, dtype=np.int64)
        msg_all = np.empty(n, dtype=object)
        side_all = np.empty(n, dtype=object)
        px_all = np.empty(n, dtype=np.int64)
        sz_all = np.empty(n, dtype=np.int64)
        oid_all = np.empty(n, dtype=np.int64)
        if n_order:
            t_all[:n_order] = t_arr
            aid_all[:n_order] = aid_arr
            msg_all[:n_order] = msg
            side_all[:n_order] = side_str
            px_all[:n_order] = px_arr
            sz_all[:n_order] = sz_arr
            oid_all[:n_order] = oid_arr
        if n_quote:
            t_all[n_order:] = q_t
            aid_all[n_order:] = q_aid
            msg_all[n_order:] = q_msg
            side_all[n_order:] = q_side
            px_all[n_order:] = q_px
            sz_all[n_order:] = q_sz
            oid_all[n_order:] = q_oid
        idx = _stable_lexsort(t_all, oid_all)
        df = pd.DataFrame(
            {
                "t_ns": t_all[idx],
                "agent_id": aid_all[idx],
                "msg_type": msg_all[idx],
                "side": side_all[idx],
                "price": px_all[idx],
                "size": sz_all[idx],
                "order_id": oid_all[idx],
            }
        )
        return df.astype(_TRACE_DTYPES, copy=False)


cdef class CLedger:
    """Delivered-message columns in a C struct array. pandas only at write."""

    cdef LRow *rows
    cdef Py_ssize_t n
    cdef Py_ssize_t cap

    def __cinit__(self):
        self.rows = NULL
        self.n = 0
        self.cap = 0

    def __dealloc__(self):
        if self.rows != NULL:
            free(self.rows)
            self.rows = NULL

    cdef void _grow(self) except *:
        cdef Py_ssize_t newcap = 64 if self.cap == 0 else self.cap * 2
        cdef LRow *nb = <LRow *>realloc(self.rows, newcap * sizeof(LRow))
        if nb == NULL:
            raise MemoryError()
        self.rows = nb
        self.cap = newcap

    def append(
        self,
        mid,
        src,
        dst,
        t_send,
        t_recv,
        lat,
        mtype,
        oid,
        parent,
        seq=-1,
    ):
        cdef Py_ssize_t i
        cdef LRow *r
        if self.n >= self.cap:
            self._grow()
        i = self.n
        r = &self.rows[i]
        r.mid = mid
        r.src = src
        r.dst = dst
        r.t_recv = t_recv
        r.lat = lat
        r.mtype = _mtype_code(mtype)
        r.seq = seq
        r.flags = 0
        if t_send is None:
            r.t_send = 0
            r.flags |= LF_SEND_NA
        else:
            r.t_send = t_send
        if oid is None:
            r.oid = 0
            r.flags |= LF_OID_NA
        else:
            r.oid = oid
        if parent is None:
            r.parent = 0
            r.flags |= LF_PARENT_NA
        else:
            r.parent = parent
        self.n = i + 1
        return i

    def set_seq(self, idx, seq):
        self.rows[idx].seq = seq

    def to_dataframe(self):
        from fast_sim.extract import _empty_msg, _nullable_int64
        from abides_fork.trace import _MSG_DTYPES
        import numpy as np
        import pandas as pd

        cdef Py_ssize_t i, n
        cdef LRow *r
        n = self.n
        if n == 0:
            return _empty_msg()
        seq = np.empty(n, dtype=np.int64)
        for i in range(n):
            seq[i] = self.rows[i].seq
        keep = seq >= 0
        if not keep.any():
            return _empty_msg()
        mid = np.empty(n, dtype=np.int64)
        src = np.empty(n, dtype=np.int32)
        dst = np.empty(n, dtype=np.int32)
        t_recv = np.empty(n, dtype=np.int64)
        lat = np.empty(n, dtype=np.int64)
        t_send = np.empty(n, dtype=np.int64)
        t_send_na = np.zeros(n, dtype=np.bool_)
        oid = np.empty(n, dtype=np.int64)
        oid_na = np.zeros(n, dtype=np.bool_)
        parent = np.empty(n, dtype=np.int64)
        parent_na = np.zeros(n, dtype=np.bool_)
        mtype = np.empty(n, dtype=object)
        names = _MT_NAMES
        for i in range(n):
            r = &self.rows[i]
            mid[i] = r.mid
            src[i] = r.src
            dst[i] = r.dst
            t_recv[i] = r.t_recv
            lat[i] = r.lat
            t_send[i] = r.t_send
            oid[i] = r.oid
            parent[i] = r.parent
            t_send_na[i] = bool(r.flags & LF_SEND_NA)
            oid_na[i] = bool(r.flags & LF_OID_NA)
            parent_na[i] = bool(r.flags & LF_PARENT_NA)
            mt = r.mtype
            mtype[i] = names[mt] if 0 <= mt < len(names) else "AGENT_WAKEUP"
        order = np.argsort(seq[keep], kind="stable")
        mid = mid[keep][order]
        src = src[keep][order]
        dst = dst[keep][order]
        t_recv = t_recv[keep][order]
        lat = lat[keep][order]
        mtype = mtype[keep][order]
        seq_out = seq[keep][order]
        t_send = t_send[keep][order]
        t_send_na = t_send_na[keep][order]
        oid = oid[keep][order]
        oid_na = oid_na[keep][order]
        parent = parent[keep][order]
        parent_na = parent_na[keep][order]
        df = pd.DataFrame(
            {
                "seq": seq_out,
                "t_recv_ns": t_recv,
                "t_send_ns": _nullable_int64(t_send, t_send_na),
                "latency_ns": lat,
                "src_id": src,
                "dst_id": dst,
                "message_id": mid,
                "msg_type": mtype,
                "order_id": _nullable_int64(oid, oid_na),
                "causal_parent": _nullable_int64(parent, parent_na),
            }
        )
        return df.astype(_MSG_DTYPES, copy=False)


cdef int _co_grow(COrder **buf, Py_ssize_t *cap, Py_ssize_t need) except -1:
    cdef Py_ssize_t newcap
    cdef COrder *nb
    if need <= cap[0]:
        return 0
    newcap = 4 if cap[0] == 0 else cap[0] * 2
    while newcap < need:
        newcap *= 2
    nb = <COrder *>realloc(buf[0], newcap * sizeof(COrder))
    if nb == NULL:
        raise MemoryError()
    buf[0] = nb
    cap[0] = newcap
    return 0


cdef class CPriceLevel:
    """One price: COrder arrays, no Python LimitOrder."""

    cdef public int price
    cdef public int _visible_qty
    cdef unsigned char side
    cdef COrder *vis
    cdef COrder *hid
    cdef Py_ssize_t n_vis, cap_vis, n_hid, cap_hid

    def __cinit__(self):
        self.vis = NULL
        self.hid = NULL
        self.n_vis = 0
        self.cap_vis = 0
        self.n_hid = 0
        self.cap_hid = 0
        self._visible_qty = 0
        self.price = 0
        self.side = 0

    def __dealloc__(self):
        if self.vis != NULL:
            free(self.vis)
            self.vis = NULL
        if self.hid != NULL:
            free(self.hid)
            self.hid = NULL


cdef CPriceLevel _new_clevel(COrder *order):
    cdef CPriceLevel level = CPriceLevel.__new__(CPriceLevel)
    level.price = order.limit_price
    level.side = order.side
    level._visible_qty = 0
    if order.flags & FL_HIDDEN:
        _co_grow(&level.hid, &level.cap_hid, 1)
        level.hid[0] = order[0]
        level.n_hid = 1
    else:
        _co_grow(&level.vis, &level.cap_vis, 1)
        level.vis[0] = order[0]
        level.n_vis = 1
        level._visible_qty = order.quantity
    return level


cdef void _c_vis_add(CPriceLevel lv, COrder *o) except *:
    cdef Py_ssize_t i, n
    if o.flags & FL_HIDDEN:
        _co_grow(&lv.hid, &lv.cap_hid, lv.n_hid + 1)
        lv.hid[lv.n_hid] = o[0]
        lv.n_hid += 1
        return
    if o.flags & FL_INSERT_ID:
        _co_grow(&lv.vis, &lv.cap_vis, lv.n_vis + 1)
        n = lv.n_vis
        i = 0
        while i < n and lv.vis[i].order_id <= o.order_id:
            i += 1
        if i < n:
            memmove(&lv.vis[i + 1], &lv.vis[i], (n - i) * sizeof(COrder))
        lv.vis[i] = o[0]
        lv.n_vis = n + 1
    else:
        _co_grow(&lv.vis, &lv.cap_vis, lv.n_vis + 1)
        lv.vis[lv.n_vis] = o[0]
        lv.n_vis += 1
    lv._visible_qty = lv._visible_qty + o.quantity


cdef inline bint _c_level_empty(CPriceLevel lv) noexcept:
    return lv.n_vis == 0 and lv.n_hid == 0


cdef inline bint _c_is_match(CPriceLevel lv, COrder *order, bint is_bid) noexcept:
    if is_bid:
        if order.limit_price < lv.price:
            return 0
    elif order.limit_price > lv.price:
        return 0
    if (order.flags & FL_POST_ONLY) and lv._visible_qty == 0:
        return 0
    return 1


cdef inline COrder *_c_peek(CPriceLevel lv) noexcept:
    if lv.n_vis:
        return &lv.vis[0]
    if lv.n_hid:
        return &lv.hid[0]
    return NULL


cdef int _c_pop(CPriceLevel lv, COrder *out) noexcept:
    if lv.n_vis:
        out[0] = lv.vis[0]
        lv._visible_qty = lv._visible_qty - lv.vis[0].quantity
        lv.n_vis -= 1
        if lv.n_vis:
            memmove(&lv.vis[0], &lv.vis[1], lv.n_vis * sizeof(COrder))
        return 1
    if lv.n_hid:
        out[0] = lv.hid[0]
        lv.n_hid -= 1
        if lv.n_hid:
            memmove(&lv.hid[0], &lv.hid[1], lv.n_hid * sizeof(COrder))
        return 1
    return 0


cdef int _c_remove_oid(CPriceLevel lv, long long oid, COrder *out) noexcept:
    cdef Py_ssize_t i, n
    n = lv.n_vis
    for i in range(n):
        if lv.vis[i].order_id == oid:
            out[0] = lv.vis[i]
            lv._visible_qty = lv._visible_qty - lv.vis[i].quantity
            lv.n_vis = n - 1
            if i < lv.n_vis:
                memmove(&lv.vis[i], &lv.vis[i + 1], (lv.n_vis - i) * sizeof(COrder))
            return 1
    n = lv.n_hid
    for i in range(n):
        if lv.hid[i].order_id == oid:
            out[0] = lv.hid[i]
            lv.n_hid = n - 1
            if i < lv.n_hid:
                memmove(&lv.hid[i], &lv.hid[i + 1], (lv.n_hid - i) * sizeof(COrder))
            return 1
    return 0


cdef inline object _snap_tuple(COrder *o):
    """Fill / accept / cancel snapshot: primitives only, not a LimitOrder."""
    return (o.agent_id, o.order_id, o.quantity, o.fill_price, o.limit_price, o.side)


cdef void _corder_from_tuple(COrder *o, object t) except *:
    o.agent_id = t[0]
    o.time_placed = t[1]
    o.quantity = t[3]
    o.side = 1 if t[4] is _BID else 0
    o.limit_price = t[5]
    o.order_id = t[6]
    o.fill_price = -1
    o.flags = 0
    if t[8]:
        o.flags |= FL_HIDDEN
    if t[9]:
        o.flags |= FL_PTC
    if t[10]:
        o.flags |= FL_INSERT_ID
    if len(t) > 11 and t[11]:
        o.flags |= FL_POST_ONLY


cdef void _corder_from_limit(COrder *o, object order) except *:
    o.agent_id = order.agent_id
    o.time_placed = order.time_placed
    o.quantity = order.quantity
    o.side = 1 if order.side is _BID else 0
    o.limit_price = order.limit_price
    o.order_id = order.order_id
    o.fill_price = -1 if order.fill_price is None else order.fill_price
    o.flags = 0
    if order.is_hidden:
        o.flags |= FL_HIDDEN
    if order.is_price_to_comply:
        o.flags |= FL_PTC
    if order.insert_by_id:
        o.flags |= FL_INSERT_ID
    if order.is_post_only:
        o.flags |= FL_POST_ONLY


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


cdef object _get_latency(object self, object sender_id, object recipient_id):
    if sender_id == recipient_id:
        return 0
    model = self._model
    if model == "log_normal":
        value = _agent_mt(self).lognormal(self._mu, self._sigma)
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


cdef void _send_compact(object kernel, object sender_id, object recipient_id, int kind, object payload, object delay, object type_name, object order_id) except *:
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
        latency = _get_latency(latency_model, sender_id, recipient_id)
        deliver_at = sent_time + int(latency)
    else:
        latency = kernel.agent_latency[sender_id][recipient_id]
        noise = kernel.random_state.choice(len(kernel.latency_noise), p=kernel.latency_noise)
        deliver_at = sent_time + int(latency + noise)
    kernel.messages.push_event(deliver_at, sender_id, recipient_id, mid, kind, payload)
    _ledger_send(kernel, mid, sender_id, recipient_id, sent_time, deliver_at, type_name, order_id)


def send_compact(kernel, sender_id, recipient_id, kind, payload, delay, type_name, order_id):
    _send_compact(kernel, sender_id, recipient_id, kind, payload, delay, type_name, order_id)


def _exch_send_kind(owner, recipient_id, kind, order, type_name):
    oid = order[1] if type(order) is tuple else order.order_id
    _send_compact(
        owner.kernel, owner.id, recipient_id, kind, order,
        owner.pipeline_delay, type_name, oid,
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
        # ABIDES sets this at exchange send time: current_time > mkt_close.
        # Step 3 hardcoded False and after-close QuerySpreads let agents act().
        m.mkt_closed = bool(payload[5]) if len(payload) > 5 else False
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


cdef object _limit_from_tuple(object t):
    """Rebuild a LimitOrder at the exchange. Hop payload is fields, not an object."""
    o = _LimitOrder.__new__(_LimitOrder)
    o.agent_id = t[0]
    o.time_placed = t[1]
    o.symbol = t[2]
    o.quantity = t[3]
    o.side = t[4]
    o.limit_price = t[5]
    o.order_id = t[6]
    o.fill_price = None
    o.tag = t[7]
    o.is_hidden = t[8]
    o.is_price_to_comply = t[9]
    o.insert_by_id = t[10]
    o.is_post_only = t[11]
    return o


cdef object _as_limit(object payload):
    if type(payload) is tuple:
        return _limit_from_tuple(payload)
    return payload


def _cheap_or_deepcopy(obj, memo=None):
    t = type(obj)
    if t is _LimitOrder or t is _MarketOrder:
        return cheap_clone(obj)
    if memo is None:
        return _real_deepcopy(obj)
    return _real_deepcopy(obj, memo)


def get_latency(self, sender_id, recipient_id):
    return _get_latency(self, sender_id, recipient_id)


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


cdef void _c_enter(object book, COrder *order) except *:
    cdef bint is_bid = order.side == 1
    cdef Py_ssize_t i, n
    cdef object levels, price_level
    cdef int px, lp
    cdef CPriceLevel lv
    levels = book.bids if is_bid else book.asks
    px = order.limit_price
    if not levels:
        levels.append(_new_clevel(order))
        return
    lv = <CPriceLevel>levels[-1]
    lp = lv.price
    if (is_bid and px < lp) or ((not is_bid) and px > lp):
        levels.append(_new_clevel(order))
        return
    n = len(levels)
    for i in range(n):
        lv = <CPriceLevel>levels[i]
        lp = lv.price
        if px == lp:
            _c_vis_add(lv, order)
            return
        if (is_bid and px > lp) or ((not is_bid) and px < lp):
            levels.insert(i, _new_clevel(order))
            return


cdef int _c_execute(object book, COrder *incoming, COrder *out_matched) except -1:
    cdef bint is_bid = incoming.side == 1
    cdef object levels
    cdef CPriceLevel lv
    cdef COrder *rest
    cdef COrder matched
    cdef int fill_qty
    levels = book.asks if is_bid else book.bids
    if not levels:
        return 0
    lv = <CPriceLevel>levels[0]
    if not _c_is_match(lv, incoming, is_bid):
        return 0
    rest = _c_peek(lv)
    if rest == NULL:
        return 0
    if incoming.quantity >= rest.quantity:
        _c_pop(lv, &matched)
        if _c_level_empty(lv):
            del levels[0]
    else:
        matched = rest[0]
        fill_qty = incoming.quantity
        matched.quantity = fill_qty
        rest.quantity -= fill_qty
        if (rest.flags & FL_HIDDEN) == 0:
            lv._visible_qty = lv._visible_qty - fill_qty
    matched.fill_price = matched.limit_price
    out_matched[0] = matched
    return 1


cdef void _c_send_snap(object owner, int recipient_id, int kind, COrder *o, object type_name) except *:
    _send_compact(
        owner.kernel, owner.id, recipient_id, kind, _snap_tuple(o),
        owner.pipeline_delay, type_name, o.order_id,
    )


cdef bint _c_cancel(object book, bint is_bid, int px, long long oid, int agent_id, bint quiet) except -1:
    cdef object levels
    cdef Py_ssize_t i, n
    cdef CPriceLevel lv
    cdef COrder cancelled
    levels = book.bids if is_bid else book.asks
    if not levels:
        return 0
    n = len(levels)
    for i in range(n):
        lv = <CPriceLevel>levels[i]
        if lv.price != px:
            continue
        if not _c_remove_oid(lv, oid, &cancelled):
            continue
        if _c_level_empty(lv):
            del levels[i]
        if not quiet:
            _c_send_snap(book.owner, agent_id, 4, &cancelled, "OrderCancelledMsg")
        book.last_update_ts = book.owner.current_time
        return 1
    return 0


cdef void _c_handle_limit(object book, COrder *incoming, bint quiet) except *:
    cdef bint is_bid = incoming.side == 1
    cdef object owner = book.owner
    cdef object opp, stp
    cdef CPriceLevel lv
    cdef COrder *rest
    cdef COrder matched
    cdef COrder fill
    cdef COrder ack
    cdef int trade_qty = 0
    cdef long long trade_px_sum = 0
    cdef object tr
    cdef int eid

    while True:
        stp = getattr(owner, "stp_policy", None)
        if stp:
            opp = book.asks if is_bid else book.bids
            if opp:
                lv = <CPriceLevel>opp[0]
                if _c_is_match(lv, incoming, is_bid):
                    rest = _c_peek(lv)
                    if rest != NULL and rest.agent_id == incoming.agent_id:
                        if stp == "cancel_oldest" and _c_cancel(
                            book, rest.side == 1, rest.limit_price,
                            rest.order_id, rest.agent_id, quiet,
                        ):
                            continue
                        if stp != "cancel_oldest":
                            if not quiet:
                                ack = incoming[0]
                                _c_send_snap(owner, incoming.agent_id, 4, &ack, "OrderCancelledMsg")
                            break
        if _c_execute(book, incoming, &matched):
            fill = incoming[0]
            fill.quantity = matched.quantity
            fill.fill_price = matched.fill_price
            incoming.quantity -= matched.quantity
            if not quiet:
                _c_send_snap(owner, matched.agent_id, 2, &matched, "OrderExecutedMsg")
                _c_send_snap(owner, incoming.agent_id, 2, &fill, "OrderExecutedMsg")
            trade_qty += matched.quantity
            trade_px_sum += <long long>matched.fill_price * matched.quantity
            if incoming.quantity <= 0:
                break
        else:
            _c_enter(book, incoming)
            if not quiet:
                _c_send_snap(owner, incoming.agent_id, 3, incoming, "OrderAcceptedMsg")
            break

    now = owner.current_time
    tr = getattr(getattr(owner, "kernel", None), "_col_trace", None)
    if tr is not None:
        eid = owner.id
        if book.bids:
            lv = <CPriceLevel>book.bids[0]
            if type(tr) is CTrace:
                (<CTrace>tr).add_quote_c(now, 1, lv.price, lv._visible_qty, eid)
            else:
                tr.add_quote(now, True, lv.price, lv._visible_qty, eid)
        if book.asks:
            lv = <CPriceLevel>book.asks[0]
            if type(tr) is CTrace:
                (<CTrace>tr).add_quote_c(now, 0, lv.price, lv._visible_qty, eid)
            else:
                tr.add_quote(now, False, lv.price, lv._visible_qty, eid)
    else:
        log = owner.log
        if book.bids:
            lv = <CPriceLevel>book.bids[0]
            log.append((now, "BEST_BID", lv.price, lv._visible_qty))
        if book.asks:
            lv = <CPriceLevel>book.asks[0]
            log.append((now, "BEST_ASK", lv.price, lv._visible_qty))
    if trade_qty:
        book.last_trade = int(round(trade_px_sum / trade_qty))


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
    cdef COrder incoming
    if type(order) is tuple:
        if order[2] != self.symbol or order[3] <= 0 or order[5] < 0:
            return
        _corder_from_tuple(&incoming, order)
    else:
        if order.symbol != self.symbol or order.quantity <= 0 or order.limit_price < 0:
            return
        _corder_from_limit(&incoming, order)
    _c_handle_limit(self, &incoming, quiet)


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
    cdef bint is_bid
    cdef int px
    cdef long long oid
    cdef int agent_id
    cdef Py_ssize_t i, n
    if type(order) is tuple:
        is_bid = order[5] == 1
        px = order[4]
        oid = order[1]
        agent_id = order[0]
    else:
        is_bid = _side_is_bid(order)
        px = order.limit_price
        oid = order.order_id
        agent_id = order.agent_id
    levels = self.bids if is_bid else self.asks
    if levels and type(levels[0]) is CPriceLevel:
        return _c_cancel(self, is_bid, px, oid, agent_id, quiet)
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
            message.symbol, message.depth, message.bids, message.asks,
            message.last_trade, message.mkt_closed,
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
            agent = agents[recipient_id]
            cw = getattr(agent, "_c_wakeup", None)
            if cw is None:
                cw = 1 if getattr(agent, "interval_ns", None) is not None else 0
                agent._c_wakeup = cw
            if cw:
                sched_wakeup(agent, self.current_time)
                wakeup_result = None
            else:
                wakeup_result = agent.wakeup(self.current_time)
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
        elif kind == 2 or kind == 3 or kind == 4 or kind == 8:
            sched_receive_compact(
                agents[recipient_id], self.current_time, sender_id, kind, payload
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


cdef void _exch_receive_compact(object self, object current_time, object sender_id, int kind, object payload, object mid) except *:
    # QueryMsg is allowed after close (final spread + mkt_closed=True).
    # Limit/cancel/market after close must become MarketClosedMsg via the
    # original ExchangeAgent — that is the Step 3 miss if we answer them
    # with a False QuerySpreadResponse.
    closed = current_time > self.mkt_close
    if closed and kind != 7:
        if kind == 5:
            payload = _as_limit(payload)
        _py_exch_recv_fallback(
            self, current_time, sender_id, _materialize(kind, payload, mid)
        )
        return
    self.current_time = current_time
    self.kernel.agent_computation_delays[self.id] = self.computation_delay
    if kind == 5:
        symbol = payload[2] if type(payload) is tuple else payload.symbol
        book = self.order_books.get(symbol)
        if book is not None:
            handle_limit_order(book, payload)
            if self.data_subscriptions:
                self.publish_order_book_data()
        return
    if kind == 7:
        symbol, depth = payload
        book = self.order_books.get(symbol)
        if book is not None:
            _send_compact(
                self.kernel,
                self.id,
                sender_id,
                8,
                (
                    symbol,
                    depth,
                    _l2(book.bids, depth),
                    _l2(book.asks, depth),
                    book.last_trade,
                    closed,
                ),
                0,
                "QuerySpreadResponseMsg",
                None,
            )
        return
    if kind == 6:
        order, tag, metadata = payload
        book = self.order_books.get(order.symbol)
        if book is not None:
            # Cancel looks up by order_id; incoming object is not mutated.
            book.cancel_order(order, tag, metadata)
            if self.data_subscriptions:
                self.publish_order_book_data()
        return
    if kind == 9:
        book = self.order_books.get(payload.symbol)
        if book is not None:
            book.handle_market_order(payload)
            if self.data_subscriptions:
                self.publish_order_book_data()
        return


def exch_receive_compact(self, current_time, sender_id, kind, payload, mid):
    _exch_receive_compact(self, current_time, sender_id, kind, payload, mid)


cdef void _sched_wakeup(object self, object current_time) except *:
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
    _send_compact(
        self.kernel, self.id, self.exchange_id, 7, (self.symbol, 1),
        0, "QuerySpreadMsg", None,
    )
    self.state = "AWAITING_SPREAD"


def sched_wakeup(self, current_time):
    _sched_wakeup(self, current_time)


cdef void _place_limit_order(
    object self,
    object symbol,
    object quantity,
    object side,
    object limit_price,
    object order_id=None,
    bint is_hidden=False,
    bint is_price_to_comply=False,
    bint insert_by_id=False,
    bint is_post_only=False,
    bint ignore_risk=True,
    object tag=None,
) except *:
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
    # Agent keeps this object. Hop is a field tuple so matching cannot
    # mutate self.orders (execute_order would otherwise double-decrement).
    self.orders[order.order_id] = order
    _send_compact(
        self.kernel,
        self.id,
        self.exchange_id,
        5,
        (
            order.agent_id,
            order.time_placed,
            order.symbol,
            order.quantity,
            order.side,
            order.limit_price,
            order.order_id,
            order.tag,
            order.is_hidden,
            order.is_price_to_comply,
            order.insert_by_id,
            order.is_post_only,
        ),
        0,
        "LimitOrderMsg",
        order.order_id,
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
    _place_limit_order(
        self, symbol, quantity, side, limit_price, order_id,
        is_hidden, is_price_to_comply, insert_by_id, is_post_only,
        ignore_risk, tag,
    )


cdef void _noise_act(object self) except *:
    symbol = self.symbol
    bids = self.known_bids.get(symbol)
    asks = self.known_asks.get(symbol)
    bid = bids[0][0] if bids else None
    ask = asks[0][0] if asks else None
    cdef MT19937 mt = _agent_mt(self)
    size = int(max(1, round(mt.normal(self.order_size_mean, self.order_size_std))))
    buy = mt.randint(0, 2)
    offset = int(mt.randint(0, self.price_offset_ticks + 1))
    if buy:
        anchor = int(ask) if ask else (int(bid) if bid else self.reference_price)
        _place_limit_order(self, symbol, size, _BID, anchor + offset)
    else:
        anchor = int(bid) if bid else (int(ask) if ask else self.reference_price)
        _place_limit_order(self, symbol, size, _ASK, anchor - offset)


def noise_act(self):
    _noise_act(self)


def ta_cancel_order(self, order, tag=None, metadata=None):
    """TradingAgent.cancel_order — compact CancelOrderMsg hop."""
    if type(order) is not _LimitOrder:
        return
    _send_compact(
        self.kernel,
        self.id,
        self.exchange_id,
        6,
        (order, tag, {} if metadata is None else metadata),
        0,
        "CancelOrderMsg",
        order.order_id,
    )


def cancel_all_orders(self):
    for order in self.orders.values():
        if type(order) is _LimitOrder:
            ta_cancel_order(self, order)


cdef void _mm_act(object self) except *:
    symbol = self.symbol
    bids = self.known_bids.get(symbol)
    asks = self.known_asks.get(symbol)
    bid = bids[0][0] if bids else None
    ask = asks[0][0] if asks else None
    mid = int((bid + ask) // 2) if (bid and ask) else self.reference_price
    cancel_all_orders(self)
    half = self.spread_ticks // 2
    size = self.size_per_level
    for lvl in range(self.depth_levels):
        _place_limit_order(self, symbol, size, _BID, mid - half - lvl)
        _place_limit_order(self, symbol, size, _ASK, mid + half + lvl)


def mm_act(self):
    _mm_act(self)


cdef void _value_act(object self) except *:
    symbol = self.symbol
    bids = self.known_bids.get(symbol)
    asks = self.known_asks.get(symbol)
    bid = bids[0][0] if bids else None
    ask = asks[0][0] if asks else None
    if bid and ask:
        mid = (int(bid) + int(ask)) / 2.0
    elif bid:
        mid = float(bid)
    elif ask:
        mid = float(ask)
    else:
        return
    # Lookup only (sigma_n=0 draws nothing). Noise is C MT19937, same
    # int(round(normal(r_t, sqrt(sigma_n)))) as oracle.observe_price.
    r_t = self.oracle.observe_price(
        symbol, self.current_time, self.random_state, sigma_n=0
    )
    if self.sigma_n:
        fundamental = int(
            round(_agent_mt(self).normal(r_t, sqrt(self.sigma_n)))
        )
    else:
        fundamental = int(r_t)
    size = int(max(1, round(self.order_size_mean)))
    if mid < fundamental - self.threshold_ticks and ask:
        _place_limit_order(self, symbol, size, _BID, int(ask))
    elif mid > fundamental + self.threshold_ticks and bid:
        _place_limit_order(self, symbol, size, _ASK, int(bid))


def value_act(self):
    _value_act(self)


cdef void _momentum_act(object self) except *:
    symbol = self.symbol
    bids = self.known_bids.get(symbol)
    asks = self.known_asks.get(symbol)
    bid = bids[0][0] if bids else None
    ask = asks[0][0] if asks else None
    if bid and ask:
        mid = (int(bid) + int(ask)) / 2.0
    elif bid:
        mid = float(bid)
    elif ask:
        mid = float(ask)
    else:
        return
    hist = self._mid_history
    hist.append(mid)
    if len(hist) > self.lookback + 1:
        hist.pop(0)
    if len(hist) <= self.lookback:
        return
    past = hist[0]
    size = int(max(1, round(self.order_size_mean)))
    if mid > past + self.threshold_ticks and ask:
        _place_limit_order(self, symbol, size, _BID, int(ask))
    elif mid < past - self.threshold_ticks and bid:
        _place_limit_order(self, symbol, size, _ASK, int(bid))


def momentum_act(self):
    _momentum_act(self)


cdef int _act_code(object agent) except -1:
    obj = getattr(agent, "_c_act", None)
    if obj is not None:
        return <int>obj
    name = type(agent).__name__
    cdef int k
    if name == "NoiseTrader":
        k = 1
    elif name == "MarketMaker":
        k = 2
    elif name == "ValueTrader":
        k = 3
    elif name == "MomentumTrader":
        k = 4
    else:
        k = 0
    agent._c_act = k
    return k


cdef void _dispatch_act(object agent) except *:
    cdef int k = _act_code(agent)
    if k == 1:
        _noise_act(agent)
    elif k == 2:
        _mm_act(agent)
    elif k == 3:
        _value_act(agent)
    elif k == 4:
        _momentum_act(agent)
    else:
        agent.act()


cdef inline void _tr_order(object self, int ev, int aid, unsigned char is_bid, int px, int sz, int oid) except *:
    cdef object tr = getattr(self.kernel, "_col_trace", None)
    if tr is None:
        side = "BID" if is_bid else "ASK"
        name = "ORDER_EXECUTED" if ev == EV_EXEC else (
            "ORDER_ACCEPTED" if ev == EV_ACCEPT else (
                "ORDER_CANCELLED" if ev == EV_CANCEL else "ORDER_SUBMITTED"
            )
        )
        self.log.append((self.current_time, name, aid, side, px, sz, oid))
        return
    if type(tr) is CTrace:
        (<CTrace>tr).add_order_c(self.current_time, ev, aid, is_bid, px, sz, oid)
    else:
        tr.add_order(
            self.current_time,
            "ORDER_EXECUTED" if ev == EV_EXEC else (
                "ORDER_ACCEPTED" if ev == EV_ACCEPT else "ORDER_CANCELLED"
            ),
            aid, "BID" if is_bid else "ASK", px, sz, oid,
        )


cdef void _ta_order_executed(object self, object order) except *:
    cdef int aid, oid, q, fp, lp
    cdef unsigned char is_bid
    if type(order) is tuple:
        aid = order[0]
        oid = order[1]
        q = order[2]
        fp = order[3]
        lp = order[4]
        is_bid = <unsigned char>order[5]
        if fp < 0:
            fp = 0
    else:
        aid = order.agent_id
        oid = order.order_id
        q = order.quantity
        fp = 0 if order.fill_price is None else order.fill_price
        is_bid = 1 if order.side is _BID else 0
    if self.log_orders:
        _tr_order(self, EV_EXEC, aid, is_bid, fp, q, oid)
    qty = q if is_bid else -q
    sym = self.symbol
    holdings = self.holdings
    if sym in holdings:
        holdings[sym] += qty
    else:
        holdings[sym] = qty
    if holdings[sym] == 0:
        del holdings[sym]
    holdings["CASH"] -= qty * fp
    orders = self.orders
    if oid in orders:
        o = orders[oid]
        if q >= o.quantity:
            del orders[oid]
        else:
            o.quantity -= q


cdef void _ta_order_accepted(object self, object order) except *:
    cdef int aid, oid, q, lp
    cdef unsigned char is_bid
    if type(order) is tuple:
        aid = order[0]
        oid = order[1]
        q = order[2]
        lp = order[4]
        is_bid = <unsigned char>order[5]
    else:
        aid = order.agent_id
        oid = order.order_id
        q = order.quantity
        lp = order.limit_price
        is_bid = 1 if order.side is _BID else 0
    if self.log_orders:
        _tr_order(self, EV_ACCEPT, aid, is_bid, lp, q, oid)


cdef void _ta_order_cancelled(object self, object order) except *:
    cdef int aid, oid, q, lp
    cdef unsigned char is_bid
    if type(order) is tuple:
        aid = order[0]
        oid = order[1]
        q = order[2]
        lp = order[4]
        is_bid = <unsigned char>order[5]
    else:
        aid = order.agent_id
        oid = order.order_id
        q = order.quantity
        lp = order.limit_price
        is_bid = 1 if order.side is _BID else 0
    if self.log_orders:
        _tr_order(self, EV_CANCEL, aid, is_bid, lp, q, oid)
    orders = self.orders
    if oid in orders:
        del orders[oid]


cdef void _sched_receive_compact(object self, object current_time, object sender_id, int kind, object payload) except *:
    """Dispatch exec/accept/cancel/spread without allocating a Python Message."""
    self.current_time = current_time
    if kind == 8:
        # payload: (symbol, depth, bids, asks, last_trade, mkt_closed)
        mkt_closed = bool(payload[5]) if len(payload) > 5 else False
        if mkt_closed:
            self.mkt_closed = True
        symbol = payload[0]
        last_trade = payload[4]
        self.last_trade[symbol] = last_trade
        if self.mkt_closed:
            self.daily_close_price[symbol] = last_trade
        self.known_bids[symbol] = payload[2]
        self.known_asks[symbol] = payload[3]
        self.book = ""
        if self.state == "AWAITING_SPREAD":
            if not self.mkt_closed:
                _dispatch_act(self)
            self.state = "AWAITING_WAKEUP"
        return
    if kind == 2:
        _ta_order_executed(self, payload)
        return
    if kind == 3:
        _ta_order_accepted(self, payload)
        return
    if kind == 4:
        _ta_order_cancelled(self, payload)
        return


def sched_receive_compact(self, current_time, sender_id, kind, payload):
    _sched_receive_compact(self, current_time, sender_id, kind, payload)


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


def mt19937_matches_numpy(seeds=20, draws=50):
    """True iff C MT19937 lock-steps numpy 1.26 RandomState draw sequences.

    Covers NoiseTrader (normal + randint), latency lognormal, and
    ValueTrader observe_price (int(round(normal(r_t, sqrt(sigma_n))))).
    """
    import numpy as np

    for seed in range(seeds):
        rs = np.random.RandomState(seed)
        mt = MT19937()
        mt.bind_numpy(rs)
        for i in range(draws):
            if mt.normal(10.0, 2.0) != float(rs.normal(10.0, 2.0)):
                return False
            if mt.randint(0, 2) != int(rs.randint(0, 2)):
                return False
            if mt.randint(0, 6) != int(rs.randint(0, 6)):
                return False
            mu = 6.5 + (seed % 5) * 0.1
            sigma = 0.15 + (i % 7) * 0.01
            if mt.lognormal(mu, sigma) != float(rs.lognormal(mean=mu, sigma=sigma)):
                return False
            r_t = 100000.0 + 10 * i
            sn = 1000.0 + 50 * (i % 3)
            scale = sqrt(sn)
            if int(round(mt.normal(r_t, scale))) != int(
                round(rs.normal(loc=r_t, scale=scale))
            ):
                return False
    return True
