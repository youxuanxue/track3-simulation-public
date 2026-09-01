# cython: language_level=3, boundscheck=False, wraparound=True
"""From-scratch C kernel for Exchange + Noise / MM / Value / Momentum.

Replaces ``Kernel.run``. Heap key, message_id / order_id order, MT19937,
STP, pipeline_delay and Step 5 after-close match the hybrid. No GPU.
"""

from libc.math cimport exp, log, sqrt
from libc.stdint cimport uint32_t
from libc.stdlib cimport free, malloc, realloc
from libc.string cimport memmove
cdef enum:
    KIND_WAKE = 1
    KIND_EXEC = 2
    KIND_ACCEPT = 3
    KIND_CANCEL = 4
    KIND_LIMIT = 5
    KIND_CANCEL_REQ = 6
    KIND_SPREAD_REQ = 7
    KIND_SPREAD_RESP = 8
    KIND_HOURS_REQ = 10
    KIND_HOURS_RESP = 11
    KIND_CLOSE_REQ = 12
    KIND_CLOSE_PX = 13
    KIND_MKT_CLOSED = 14

    EV_SUBMIT = 0
    EV_ACCEPT = 1
    EV_EXEC = 2
    EV_CANCEL = 3
    LF_SEND_NA = 1
    LF_OID_NA = 2
    LF_PARENT_NA = 4
    FL_HIDDEN = 1
    FL_PTC = 2
    FL_INSERT_ID = 4
    FL_POST_ONLY = 8
    _MT_N = 624
    _MT_M = 397
    _MT_MATRIX_A = 0x9908b0df
    _MT_UPPER = 0x80000000
    _MT_LOWER = 0x7fffffff
    LAT_DET = 0
    LAT_UNI = 1
    LAT_LOGN = 2
    LAT_PARETO = 3
    STP_NONE = 0
    STP_NEWEST = 1
    STP_OLDEST = 2
    ST_WAKE = 0
    ST_SPREAD = 1


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


cdef struct COrder:
    long long time_placed
    long long order_id
    int agent_id
    int quantity
    int limit_price
    int fill_price
    unsigned char side
    unsigned char flags


cdef struct Event:
    long long deliver_at
    long long message_id
    int sender_id
    int recipient_id
    int kind
    COrder order
    int extra
    int bid_px
    int bid_qty
    int ask_px
    int ask_qty
    unsigned char mkt_closed
    unsigned char has_bid
    unsigned char has_ask


cdef struct NAgent:
    int id
    int kind
    int first_wake
    int mkt_closed
    int has_hours
    int state
    int log_orders
    int exchange_id
    long long interval_ns
    long long mkt_open
    long long mkt_close
    int bid_px
    int bid_qty
    int ask_px
    int ask_qty
    unsigned char has_bid
    unsigned char has_ask
    int last_trade
    int reference_price
    double order_size_mean
    double order_size_std
    int price_offset_ticks
    int spread_ticks
    int depth_levels
    int size_per_level
    double threshold_ticks
    double sigma_n
    int lookback
    double *mid_hist
    int n_mid
    int cap_mid
    COrder *orders
    Py_ssize_t n_ord
    Py_ssize_t cap_ord


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


cdef class MT19937:
    cdef uint32_t key[624]
    cdef int pos
    cdef int has_gauss
    cdef double gauss

    def bind_numpy(self, rs):
        st = rs.get_state()
        cdef Py_ssize_t i
        for i in range(624):
            self.key[i] = <uint32_t>st[1][i]
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
            self.key[i] = self.key[i + (_MT_M - _MT_N)] ^ (y >> 1) ^ ((-(y & 1)) & _MT_MATRIX_A)
        y = (self.key[_MT_N - 1] & _MT_UPPER) | (self.key[0] & _MT_LOWER)
        self.key[_MT_N - 1] = self.key[_MT_M - 1] ^ (y >> 1) ^ ((-(y & 1)) & _MT_MATRIX_A)
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
        return ((self.next32() >> 5) * 67108864.0 + (self.next32() >> 6)) / 9007199254740992.0

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


cdef inline int _event_less(Event *a, Event *b) noexcept nogil:
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
        if self.buf != NULL:
            free(self.buf)
            self.buf = NULL

    cdef int _grow(self) except -1:
        cdef Event *nb = <Event *>realloc(self.buf, self.cap * 2 * sizeof(Event))
        if nb == NULL:
            raise MemoryError()
        self.buf = nb
        self.cap *= 2
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
        cdef Py_ssize_t l, r, smallest
        cdef Event tmp
        cdef Py_ssize_t n = self.n
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

    cdef void _push_raw(self, Event ev) except *:
        if self.n >= self.cap:
            self._grow()
        self.buf[self.n] = ev
        self.n += 1
        self._sift_up(self.n - 1)

    cdef Event pop_ev(self) except *:
        if self.n == 0:
            raise IndexError("empty")
        cdef Event top = self.buf[0]
        self.n -= 1
        if self.n > 0:
            self.buf[0] = self.buf[self.n]
            self._sift_down(0)
        return top

    cdef bint empty(self) noexcept:
        return self.n == 0


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


cdef void _ord_put(NAgent *a, COrder *o) except *:
    cdef Py_ssize_t i
    for i in range(a.n_ord):
        if a.orders[i].order_id == o.order_id:
            a.orders[i] = o[0]
            return
    _co_grow(&a.orders, &a.cap_ord, a.n_ord + 1)
    a.orders[a.n_ord] = o[0]
    a.n_ord += 1


cdef int _ord_remove(NAgent *a, long long oid) noexcept:
    cdef Py_ssize_t i
    for i in range(a.n_ord):
        if a.orders[i].order_id == oid:
            a.n_ord -= 1
            if i < a.n_ord:
                memmove(&a.orders[i], &a.orders[i + 1], <size_t>(a.n_ord - i) * sizeof(COrder))
            return 1
    return 0


cdef int _ord_fill(NAgent *a, long long oid, int qty) noexcept:
    cdef Py_ssize_t i
    for i in range(a.n_ord):
        if a.orders[i].order_id == oid:
            if qty >= a.orders[i].quantity:
                return _ord_remove(a, oid)
            a.orders[i].quantity -= qty
            return 1
    return 0


cdef class CTrace:
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
        self.ot = self.oev = self.oaid = self.opx = self.osz = self.ooid = NULL
        self.oside = NULL
        self.qt = self.qaid = self.qpx = self.qsz = NULL
        self.qside = NULL
        self.n_o = self.cap_o = self.n_q = self.cap_q = 0

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

    cdef void _grow_o(self) except *:
        cdef Py_ssize_t c = 64 if self.cap_o == 0 else self.cap_o * 2
        self.ot = <long long *>realloc(self.ot, c * sizeof(long long))
        self.oev = <int *>realloc(self.oev, c * sizeof(int))
        self.oaid = <int *>realloc(self.oaid, c * sizeof(int))
        self.oside = <unsigned char *>realloc(self.oside, c * sizeof(unsigned char))
        self.opx = <int *>realloc(self.opx, c * sizeof(int))
        self.osz = <int *>realloc(self.osz, c * sizeof(int))
        self.ooid = <int *>realloc(self.ooid, c * sizeof(int))
        if self.ot == NULL or self.oev == NULL:
            raise MemoryError()
        self.cap_o = c

    cdef void _grow_q(self) except *:
        cdef Py_ssize_t c = 64 if self.cap_q == 0 else self.cap_q * 2
        self.qt = <long long *>realloc(self.qt, c * sizeof(long long))
        self.qaid = <int *>realloc(self.qaid, c * sizeof(int))
        self.qside = <unsigned char *>realloc(self.qside, c * sizeof(unsigned char))
        self.qpx = <int *>realloc(self.qpx, c * sizeof(int))
        self.qsz = <int *>realloc(self.qsz, c * sizeof(int))
        if self.qt == NULL:
            raise MemoryError()
        self.cap_q = c

    cdef void add_order_c(self, long long t, int ev, int aid, unsigned char is_bid, int px, int sz, int oid) except *:
        if self.n_o >= self.cap_o:
            self._grow_o()
        cdef Py_ssize_t i = self.n_o
        self.ot[i] = t
        self.oev[i] = ev
        self.oaid[i] = aid
        self.oside[i] = is_bid
        self.opx[i] = px
        self.osz[i] = sz
        self.ooid[i] = oid
        self.n_o = i + 1

    cdef void add_quote_c(self, long long t, unsigned char is_bid, int px, int sz, int aid) except *:
        if self.n_q >= self.cap_q:
            self._grow_q()
        cdef Py_ssize_t i = self.n_q
        self.qt[i] = t
        self.qaid[i] = aid
        self.qside[i] = is_bid
        self.qpx[i] = px
        self.qsz[i] = sz
        self.n_q = i + 1

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
            last_exec = {}
            for pos in np.nonzero(ev_arr == EV_EXEC)[0]:
                last_exec[int(oid_arr[pos])] = int(pos)
            msg = np.empty(n_order, dtype=object)
            side_str = np.empty(n_order, dtype=object)
            for i in range(n_order):
                ev = int(ev_arr[i])
                if ev == EV_EXEC:
                    msg[i] = "ORDER_FILLED" if last_exec.get(int(oid_arr[i])) == i else "PARTIAL_FILL"
                elif ev == EV_SUBMIT:
                    msg[i] = "ORDER_SUBMITTED"
                elif ev == EV_ACCEPT:
                    msg[i] = "ORDER_ACCEPTED"
                elif ev == EV_CANCEL:
                    msg[i] = "ORDER_CANCELLED"
                else:
                    msg[i] = "ORDER_REPLACED"
                side_str[i] = "BID" if side_b[i] else "ASK"
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
            oid_all[n_order:] = -1
        idx = _stable_lexsort(t_all, oid_all)
        return pd.DataFrame({
            "t_ns": t_all[idx],
            "agent_id": aid_all[idx],
            "msg_type": msg_all[idx],
            "side": side_all[idx],
            "price": px_all[idx],
            "size": sz_all[idx],
            "order_id": oid_all[idx],
        }).astype(_TRACE_DTYPES, copy=False)


cdef class CLedger:
    cdef LRow *rows
    cdef Py_ssize_t n, cap

    def __cinit__(self):
        self.rows = NULL
        self.n = self.cap = 0

    def __dealloc__(self):
        if self.rows != NULL:
            free(self.rows)

    cdef void _grow(self) except *:
        cdef Py_ssize_t c = 64 if self.cap == 0 else self.cap * 2
        cdef LRow *nb = <LRow *>realloc(self.rows, c * sizeof(LRow))
        if nb == NULL:
            raise MemoryError()
        self.rows = nb
        self.cap = c

    cdef Py_ssize_t append_c(
        self, long long mid, int src, int dst, long long t_send, long long t_recv,
        long long lat, int mtype, long long oid, long long parent, int seq,
        unsigned char flags,
    ) except -1:
        if self.n >= self.cap:
            self._grow()
        cdef Py_ssize_t i = self.n
        cdef LRow *r = &self.rows[i]
        r.mid = mid
        r.src = src
        r.dst = dst
        r.t_send = t_send
        r.t_recv = t_recv
        r.lat = lat
        r.mtype = mtype
        r.oid = oid
        r.parent = parent
        r.seq = seq
        r.flags = flags
        self.n = i + 1
        return i

    def to_dataframe(self):
        from fast_sim.extract import _empty_msg, _nullable_int64
        from abides_fork.trace import _MSG_DTYPES
        import numpy as np
        import pandas as pd

        cdef Py_ssize_t i, n = self.n
        cdef LRow *r
        if n == 0:
            return _empty_msg()
        seq = np.empty(n, dtype=np.int64)
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
            seq[i] = r.seq
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
        keep = seq >= 0
        if not keep.any():
            return _empty_msg()
        order = np.argsort(seq[keep], kind="stable")
        return pd.DataFrame({
            "seq": seq[keep][order],
            "t_recv_ns": t_recv[keep][order],
            "t_send_ns": _nullable_int64(t_send[keep][order], t_send_na[keep][order]),
            "latency_ns": lat[keep][order],
            "src_id": src[keep][order],
            "dst_id": dst[keep][order],
            "message_id": mid[keep][order],
            "msg_type": mtype[keep][order],
            "order_id": _nullable_int64(oid[keep][order], oid_na[keep][order]),
            "causal_parent": _nullable_int64(parent[keep][order], parent_na[keep][order]),
        }).astype(_MSG_DTYPES, copy=False)


cdef class CPriceLevel:
    cdef public int price
    cdef public int _visible_qty
    cdef unsigned char side
    cdef COrder *vis
    cdef COrder *hid
    cdef Py_ssize_t n_vis, cap_vis, n_hid, cap_hid

    def __cinit__(self):
        self.vis = self.hid = NULL
        self.n_vis = self.cap_vis = self.n_hid = self.cap_hid = 0
        self._visible_qty = self.price = 0
        self.side = 0

    def __dealloc__(self):
        if self.vis != NULL:
            free(self.vis)
        if self.hid != NULL:
            free(self.hid)


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


cdef class NativeSim:
    cdef EventQueue q
    cdef CTrace trace
    cdef CLedger ledger
    cdef NAgent *agents
    cdef int n
    cdef object bids
    cdef object asks
    cdef object pending
    cdef object mts
    cdef object lat_rs
    cdef object oracle
    cdef object lat_mt
    cdef object rss
    cdef long long *atime
    cdef long long *cdelay
    cdef long long now
    cdef long long start_time
    cdef long long stop_time
    cdef long long mkt_open
    cdef long long mkt_close
    cdef long long next_mid
    cdef long long next_oid
    cdef long long causal
    cdef long long seq
    cdef long long pipeline
    cdef int last_trade
    cdef int exch_cdelay
    cdef int default_cdelay
    cdef int stp
    cdef int lat_kind
    cdef double lat_min
    cdef double lat_max
    cdef double lat_mean
    cdef double lat_sigma
    cdef double lat_mu
    cdef double lat_alpha
    cdef int *subs
    cdef int n_sub
    cdef int cap_sub

    def __cinit__(self):
        self.agents = NULL
        self.atime = NULL
        self.cdelay = NULL
        self.subs = NULL
        self.n = 0
        self.n_sub = 0
        self.cap_sub = 0
        self.next_mid = 1
        self.next_oid = 0
        self.causal = 0
        self.seq = 0
        self.now = 0
        self.q = EventQueue()
        self.trace = CTrace()
        self.ledger = CLedger()
        self.bids = []
        self.asks = []
        self.pending = {}
        self.mts = []
        self.lat_rs = None
        self.oracle = None
        self.lat_mt = None
        self.rss = []

    def __dealloc__(self):
        cdef int i
        if self.agents != NULL:
            for i in range(self.n):
                if self.agents[i].orders != NULL:
                    free(self.agents[i].orders)
                if self.agents[i].mid_hist != NULL:
                    free(self.agents[i].mid_hist)
            free(self.agents)
        if self.atime != NULL:
            free(self.atime)
        if self.cdelay != NULL:
            free(self.cdelay)
        if self.subs != NULL:
            free(self.subs)

    cdef void _zero_ev(self, Event *ev) noexcept:
        ev.order.time_placed = 0
        ev.order.order_id = 0
        ev.order.agent_id = 0
        ev.order.quantity = 0
        ev.order.limit_price = 0
        ev.order.fill_price = -1
        ev.order.side = 0
        ev.order.flags = 0
        ev.extra = 0
        ev.bid_px = ev.bid_qty = ev.ask_px = ev.ask_qty = 0
        ev.mkt_closed = ev.has_bid = ev.has_ask = 0

    cdef long long _latency(self, int sid, int rid) except *:
        cdef double value
        if sid == rid:
            return 0
        if self.lat_kind == LAT_UNI:
            value = self.lat_rs.uniform(self.lat_min, self.lat_max)
        elif self.lat_kind == LAT_LOGN:
            value = (<MT19937>self.lat_mt).lognormal(self.lat_mu, self.lat_sigma)
        elif self.lat_kind == LAT_PARETO:
            value = (self.lat_min if self.lat_min > 0 else 1.0) * (1.0 + self.lat_rs.pareto(self.lat_alpha))
        else:
            value = self.lat_mean
        if value < self.lat_min:
            value = self.lat_min
        elif value > self.lat_max:
            value = self.lat_max
        return <long long>int(round(float(value)))

    cdef void _enq(self, int sid, int rid, int kind, Event *ev, long long delay, int mtype, long long oid, bint oid_na) except *:
        cdef long long mid, sent, lat, deliver
        cdef unsigned char flags
        cdef Py_ssize_t idx
        mid = self.next_mid
        self.next_mid += 1
        sent = self.now + self.cdelay[sid] + delay
        lat = self._latency(sid, rid)
        deliver = sent + lat
        ev.deliver_at = deliver
        ev.sender_id = sid
        ev.recipient_id = rid
        ev.message_id = mid
        ev.kind = kind
        self.q._push_raw(ev[0])
        flags = 0
        if oid_na:
            flags |= LF_OID_NA
        if self.causal == 0:
            flags |= LF_PARENT_NA
        idx = self.ledger.append_c(
            mid, sid, rid, sent, deliver, lat, mtype, oid, self.causal, -1, flags,
        )
        self.pending[(mid, rid)] = idx

    cdef void _enq_mid(self, int sid, int rid, int kind, Event *ev, long long delay, int mtype, long long mid, long long oid, bint oid_na) except *:
        """Enqueue with a pre-assigned message_id (MarketClosePrice broadcast)."""
        cdef long long sent, lat, deliver
        cdef unsigned char flags
        cdef Py_ssize_t idx
        sent = self.now + self.cdelay[sid] + delay
        lat = self._latency(sid, rid)
        deliver = sent + lat
        ev.deliver_at = deliver
        ev.sender_id = sid
        ev.recipient_id = rid
        ev.message_id = mid
        ev.kind = kind
        self.q._push_raw(ev[0])
        flags = 0
        if oid_na:
            flags |= LF_OID_NA
        if self.causal == 0:
            flags |= LF_PARENT_NA
        idx = self.ledger.append_c(
            mid, sid, rid, sent, deliver, lat, mtype, oid, self.causal, -1, flags,
        )
        self.pending[(mid, rid)] = idx

    cdef void _wakeup_at(self, int aid, long long when) except *:
        cdef Event ev
        cdef long long mid
        self._zero_ev(&ev)
        mid = self.next_mid
        self.next_mid += 1
        ev.deliver_at = when
        ev.sender_id = aid
        ev.recipient_id = aid
        ev.message_id = mid
        ev.kind = KIND_WAKE
        self.q._push_raw(ev)

    cdef void _l2(self, Event *ev, bint closed) except *:
        cdef object levels
        cdef CPriceLevel lv
        ev.has_bid = ev.has_ask = 0
        ev.mkt_closed = 1 if closed else 0
        ev.extra = -1 if self.last_trade < 0 else self.last_trade
        if self.bids:
            lv = <CPriceLevel>self.bids[0]
            if lv._visible_qty > 0:
                ev.has_bid = 1
                ev.bid_px = lv.price
                ev.bid_qty = lv._visible_qty
        if self.asks:
            lv = <CPriceLevel>self.asks[0]
            if lv._visible_qty > 0:
                ev.has_ask = 1
                ev.ask_px = lv.price
                ev.ask_qty = lv._visible_qty

    cdef void _enter(self, COrder *order) except *:
        cdef bint is_bid = order.side == 1
        cdef object levels = self.bids if is_bid else self.asks
        cdef CPriceLevel lv
        cdef int px, lp
        cdef Py_ssize_t i, n
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

    cdef int _execute(self, COrder *incoming, COrder *out_matched) except -1:
        cdef bint is_bid = incoming.side == 1
        cdef object levels = self.asks if is_bid else self.bids
        cdef CPriceLevel lv
        cdef COrder *rest
        cdef COrder matched
        cdef int fill_qty
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

    cdef bint _cancel(self, bint is_bid, int px, long long oid, int agent_id, bint quiet) except -1:
        cdef object levels = self.bids if is_bid else self.asks
        cdef Py_ssize_t i, n
        cdef CPriceLevel lv
        cdef COrder cancelled
        cdef Event ev
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
                self._zero_ev(&ev)
                ev.order = cancelled
                self._enq(0, agent_id, KIND_CANCEL, &ev, self.pipeline, 3, cancelled.order_id, 0)
            return 1
        return 0

    cdef void _quotes(self) except *:
        cdef CPriceLevel lv
        if self.bids:
            lv = <CPriceLevel>self.bids[0]
            self.trace.add_quote_c(self.now, 1, lv.price, lv._visible_qty, 0)
        if self.asks:
            lv = <CPriceLevel>self.asks[0]
            self.trace.add_quote_c(self.now, 0, lv.price, lv._visible_qty, 0)

    cdef void _handle_limit(self, COrder *incoming) except *:
        cdef bint is_bid = incoming.side == 1
        cdef object opp
        cdef CPriceLevel lv
        cdef COrder *rest
        cdef COrder matched, fill, ack
        cdef Event ev
        cdef int trade_qty = 0
        cdef long long trade_px_sum = 0
        while True:
            if self.stp:
                opp = self.asks if is_bid else self.bids
                if opp:
                    lv = <CPriceLevel>opp[0]
                    if _c_is_match(lv, incoming, is_bid):
                        rest = _c_peek(lv)
                        if rest != NULL and rest.agent_id == incoming.agent_id:
                            if self.stp == STP_OLDEST and self._cancel(
                                rest.side == 1, rest.limit_price,
                                rest.order_id, rest.agent_id, False,
                            ):
                                continue
                            if self.stp != STP_OLDEST:
                                self._zero_ev(&ev)
                                ev.order = incoming[0]
                                self._enq(0, incoming.agent_id, KIND_CANCEL, &ev, self.pipeline, 3, incoming.order_id, 0)
                                break
            if self._execute(incoming, &matched):
                fill = incoming[0]
                fill.quantity = matched.quantity
                fill.fill_price = matched.fill_price
                incoming.quantity -= matched.quantity
                self._zero_ev(&ev)
                ev.order = matched
                self._enq(0, matched.agent_id, KIND_EXEC, &ev, self.pipeline, 1, matched.order_id, 0)
                self._zero_ev(&ev)
                ev.order = fill
                self._enq(0, incoming.agent_id, KIND_EXEC, &ev, self.pipeline, 1, fill.order_id, 0)
                trade_qty += matched.quantity
                trade_px_sum += <long long>matched.fill_price * matched.quantity
                if incoming.quantity <= 0:
                    break
            else:
                self._enter(incoming)
                self._zero_ev(&ev)
                ev.order = incoming[0]
                self._enq(0, incoming.agent_id, KIND_ACCEPT, &ev, self.pipeline, 2, incoming.order_id, 0)
                break
        self._quotes()
        if trade_qty:
            self.last_trade = int(round(trade_px_sum / trade_qty))

    cdef void _place(self, NAgent *a, int qty, unsigned char is_bid, int px) except *:
        cdef COrder o
        cdef Event ev
        if qty <= 0:
            return
        o.agent_id = a.id
        o.time_placed = self.now
        o.quantity = qty
        o.side = is_bid
        o.order_id = self.next_oid
        self.next_oid += 1
        o.fill_price = -1
        o.limit_price = px
        o.flags = 0
        _ord_put(a, &o)
        self._zero_ev(&ev)
        ev.order = o
        self._enq(a.id, a.exchange_id, KIND_LIMIT, &ev, 0, 4, o.order_id, 0)
        if a.log_orders:
            self.trace.add_order_c(self.now, EV_SUBMIT, a.id, is_bid, px, qty, <int>o.order_id)

    cdef void _cancel_all(self, NAgent *a) except *:
        cdef Py_ssize_t i
        cdef Event ev
        for i in range(a.n_ord):
            self._zero_ev(&ev)
            ev.order = a.orders[i]
            self._enq(a.id, a.exchange_id, KIND_CANCEL_REQ, &ev, 0, 5, a.orders[i].order_id, 0)

    cdef void _act(self, NAgent *a) except *:
        cdef int bid, ask, mid, size, offset, buy, lvl, half, fundamental
        cdef double fmid, past, r_t
        cdef MT19937 mt
        cdef double *nb
        cdef int newcap
        bid = a.bid_px if a.has_bid else 0
        ask = a.ask_px if a.has_ask else 0
        if a.kind == 1:
            mt = <MT19937>self.mts[a.id]
            size = int(max(1, round(mt.normal(a.order_size_mean, a.order_size_std))))
            buy = <int>mt.randint(0, 2)
            offset = <int>mt.randint(0, a.price_offset_ticks + 1)
            if buy:
                self._place(a, size, 1, (ask if a.has_ask else (bid if a.has_bid else a.reference_price)) + offset)
            else:
                self._place(a, size, 0, (bid if a.has_bid else (ask if a.has_ask else a.reference_price)) - offset)
            return
        if a.kind == 2:
            mid = int((bid + ask) // 2) if (a.has_bid and a.has_ask) else a.reference_price
            self._cancel_all(a)
            half = a.spread_ticks // 2
            for lvl in range(a.depth_levels):
                self._place(a, a.size_per_level, 1, mid - half - lvl)
                self._place(a, a.size_per_level, 0, mid + half + lvl)
            return
        if a.kind == 3:
            if a.has_bid and a.has_ask:
                fmid = (bid + ask) / 2.0
            elif a.has_bid:
                fmid = float(bid)
            elif a.has_ask:
                fmid = float(ask)
            else:
                return
            r_t = self.oracle.observe_price("ABM", self.now, self.rss[a.id], sigma_n=0)
            if a.sigma_n:
                fundamental = int(round((<MT19937>self.mts[a.id]).normal(r_t, sqrt(a.sigma_n))))
            else:
                fundamental = int(r_t)
            size = int(max(1, round(a.order_size_mean)))
            if fmid < fundamental - a.threshold_ticks and a.has_ask:
                self._place(a, size, 1, ask)
            elif fmid > fundamental + a.threshold_ticks and a.has_bid:
                self._place(a, size, 0, bid)
            return
        if a.kind == 4:
            if a.has_bid and a.has_ask:
                fmid = (bid + ask) / 2.0
            elif a.has_bid:
                fmid = float(bid)
            elif a.has_ask:
                fmid = float(ask)
            else:
                return
            if a.n_mid >= a.cap_mid:
                newcap = 8 if a.cap_mid == 0 else a.cap_mid * 2
                nb = <double *>realloc(a.mid_hist, newcap * sizeof(double))
                if nb == NULL:
                    raise MemoryError()
                a.mid_hist = nb
                a.cap_mid = newcap
            a.mid_hist[a.n_mid] = fmid
            a.n_mid += 1
            if a.n_mid > a.lookback + 1:
                memmove(&a.mid_hist[0], &a.mid_hist[1], (a.n_mid - 1) * sizeof(double))
                a.n_mid -= 1
            if a.n_mid <= a.lookback:
                return
            past = a.mid_hist[0]
            size = int(max(1, round(a.order_size_mean)))
            if fmid > past + a.threshold_ticks and a.has_ask:
                self._place(a, size, 1, ask)
            elif fmid < past - a.threshold_ticks and a.has_bid:
                self._place(a, size, 0, bid)

    cdef void _on_exec(self, NAgent *a, COrder *o) except *:
        cdef int fp = o.fill_price if o.fill_price >= 0 else 0
        if a.log_orders:
            self.trace.add_order_c(self.now, EV_EXEC, o.agent_id, o.side, fp, o.quantity, <int>o.order_id)
        _ord_fill(a, o.order_id, o.quantity)

    cdef void _on_accept(self, NAgent *a, COrder *o) except *:
        if a.log_orders:
            self.trace.add_order_c(self.now, EV_ACCEPT, o.agent_id, o.side, o.limit_price, o.quantity, <int>o.order_id)

    cdef void _on_cancel(self, NAgent *a, COrder *o) except *:
        if a.log_orders:
            self.trace.add_order_c(self.now, EV_CANCEL, o.agent_id, o.side, o.limit_price, o.quantity, <int>o.order_id)
        _ord_remove(a, o.order_id)

    cdef void _sched_wake(self, NAgent *a) except *:
        cdef Event ev
        if a.kind == 0:
            if self.now >= self.mkt_close:
                self._send_close_px()
            return
        if a.first_wake:
            a.first_wake = 0
            self._zero_ev(&ev)
            self._enq(a.id, a.exchange_id, KIND_CLOSE_REQ, &ev, 0, 10, 0, 1)
        if not a.has_hours:
            self._zero_ev(&ev)
            self._enq(a.id, a.exchange_id, KIND_HOURS_REQ, &ev, 0, 9, 0, 1)
            return
        if not a.mkt_close or a.mkt_closed:
            return
        self._wakeup_at(a.id, self.now + a.interval_ns)
        self._zero_ev(&ev)
        ev.extra = 1
        self._enq(a.id, a.exchange_id, KIND_SPREAD_REQ, &ev, 0, 6, 0, 1)
        a.state = ST_SPREAD

    cdef void _send_close_px(self) except *:
        # Stock ExchangeAgent.wakeup builds ONE MarketClosePriceMsg and
        # send_message's it to every subscriber — one message_id, N hops.
        cdef Event ev
        cdef int i
        cdef long long mid
        mid = self.next_mid
        self.next_mid += 1
        for i in range(self.n_sub):
            self._zero_ev(&ev)
            ev.extra = self.last_trade
            self._enq_mid(0, self.subs[i], KIND_CLOSE_PX, &ev, 0, 12, mid, 0, 1)

    cdef void _exch_recv(self, int sender, Event *ev) except *:
        cdef bint closed = self.now > self.mkt_close
        cdef Event out
        cdef int kind = ev.kind
        self.cdelay[0] = self.exch_cdelay
        if kind == KIND_HOURS_REQ:
            self.cdelay[0] = 0
            self._zero_ev(&out)
            self._enq(0, sender, KIND_HOURS_RESP, &out, 0, 11, 0, 1)
            return
        if kind == KIND_CLOSE_REQ:
            if self.n_sub >= self.cap_sub:
                self.cap_sub = 8 if self.cap_sub == 0 else self.cap_sub * 2
                self.subs = <int *>realloc(self.subs, self.cap_sub * sizeof(int))
                if self.subs == NULL:
                    raise MemoryError()
            self.subs[self.n_sub] = sender
            self.n_sub += 1
            return
        if closed and kind != KIND_SPREAD_REQ:
            self._zero_ev(&out)
            self._enq(0, sender, KIND_MKT_CLOSED, &out, 0, 13, 0, 1)
            return
        if kind == KIND_LIMIT:
            self._handle_limit(&ev.order)
            return
        if kind == KIND_SPREAD_REQ:
            self._zero_ev(&out)
            self._l2(&out, closed)
            self._enq(0, sender, KIND_SPREAD_RESP, &out, 0, 7, 0, 1)
            return
        if kind == KIND_CANCEL_REQ:
            self._cancel(ev.order.side == 1, ev.order.limit_price, ev.order.order_id, ev.order.agent_id, False)

    cdef void _agent_recv(self, NAgent *a, Event *ev) except *:
        if ev.kind == KIND_SPREAD_RESP:
            if ev.mkt_closed:
                a.mkt_closed = 1
            a.last_trade = ev.extra
            a.has_bid = ev.has_bid
            a.has_ask = ev.has_ask
            a.bid_px = ev.bid_px
            a.bid_qty = ev.bid_qty
            a.ask_px = ev.ask_px
            a.ask_qty = ev.ask_qty
            if a.state == ST_SPREAD:
                if not a.mkt_closed:
                    self._act(a)
                a.state = ST_WAKE
            return
        if ev.kind == KIND_EXEC:
            self._on_exec(a, &ev.order)
            return
        if ev.kind == KIND_ACCEPT:
            self._on_accept(a, &ev.order)
            return
        if ev.kind == KIND_CANCEL:
            self._on_cancel(a, &ev.order)
            return
        if ev.kind == KIND_HOURS_RESP:
            a.mkt_open = self.mkt_open
            a.mkt_close = self.mkt_close
            if not a.has_hours:
                a.has_hours = 1
                self._wakeup_at(a.id, a.mkt_open)
            return
        if ev.kind == KIND_CLOSE_PX:
            a.last_trade = ev.extra
            return
        if ev.kind == KIND_MKT_CLOSED:
            a.mkt_closed = 1

    cdef void _deliver_seq(self, long long mid, int rid) except *:
        cdef object key = (mid, rid)
        cdef object idx = self.pending.pop(key, None)
        if idx is not None:
            self.ledger.rows[<Py_ssize_t>idx].seq = <int>self.seq
        self.seq += 1

    def setup(self, spec):
        cdef int i, k
        cdef NAgent *a
        cdef MT19937 mt
        agents = spec["agents"]
        self.n = int(spec["n_agents"])
        self.agents = <NAgent *>malloc(self.n * sizeof(NAgent))
        self.atime = <long long *>malloc(self.n * sizeof(long long))
        self.cdelay = <long long *>malloc(self.n * sizeof(long long))
        if self.agents == NULL or self.atime == NULL or self.cdelay == NULL:
            raise MemoryError()
        self.start_time = spec["start_time"]
        self.stop_time = spec["stop_time"]
        self.mkt_open = spec["mkt_open"]
        self.mkt_close = spec["mkt_close"]
        self.default_cdelay = spec["default_computation_delay"]
        self.exch_cdelay = spec["exchange_computation_delay"]
        self.pipeline = spec["pipeline_delay"]
        self.stp = spec["stp"]
        self.last_trade = spec["last_trade"]
        self.oracle = spec.get("oracle")
        lat = spec["latency"]
        model = lat["model"]
        if model == "uniform":
            self.lat_kind = LAT_UNI
        elif model == "log_normal":
            self.lat_kind = LAT_LOGN
        elif model == "pareto":
            self.lat_kind = LAT_PARETO
        else:
            self.lat_kind = LAT_DET
        self.lat_min = lat["min_ns"]
        self.lat_max = lat["max_ns"]
        self.lat_mean = lat["mean_ns"]
        self.lat_sigma = lat["sigma"]
        self.lat_mu = lat["mu"]
        self.lat_alpha = lat["alpha"]
        self.lat_rs = lat["random_state"]
        if self.lat_kind == LAT_LOGN:
            mt = MT19937()
            mt.bind_numpy(self.lat_rs)
            self.lat_mt = mt
        self.mts = [None] * self.n
        self.rss = [None] * self.n
        for i in range(self.n):
            row = agents[i]
            a = &self.agents[i]
            a.id = row["id"]
            a.kind = row["kind"]
            a.first_wake = 1
            a.mkt_closed = 0
            a.has_hours = 0
            a.state = ST_WAKE
            a.log_orders = 1 if row["log_orders"] else 0
            a.exchange_id = 0
            a.interval_ns = row["interval_ns"]
            a.mkt_open = 0
            a.mkt_close = 0
            a.has_bid = a.has_ask = 0
            a.bid_px = a.bid_qty = a.ask_px = a.ask_qty = 0
            a.last_trade = self.last_trade
            a.reference_price = row["reference_price"]
            a.order_size_mean = row["order_size_mean"]
            a.order_size_std = row["order_size_std"]
            a.price_offset_ticks = row["price_offset_ticks"]
            a.spread_ticks = row["spread_ticks"]
            a.depth_levels = row["depth_levels"]
            a.size_per_level = row["size_per_level"]
            a.threshold_ticks = row["threshold_ticks"]
            a.sigma_n = row["sigma_n"]
            a.lookback = row["lookback"]
            a.mid_hist = NULL
            a.n_mid = a.cap_mid = 0
            a.orders = NULL
            a.n_ord = a.cap_ord = 0
            self.atime[i] = 0
            self.cdelay[i] = self.default_cdelay
            rs = row["random_state"]
            self.rss[i] = rs
            if rs is not None and a.kind in (1, 3):
                mt = MT19937()
                mt.bind_numpy(rs)
                self.mts[i] = mt
        # initialize(): exchange close wakeup (mid=1), then start-time wakeups
        self.now = 0
        self._wakeup_at(0, self.mkt_close)
        for i in range(self.n):
            self._wakeup_at(i, self.start_time)
        self.now = self.start_time

    def run_loop(self):
        cdef Event ev
        cdef int kind, sid, rid
        cdef long long mid, busy
        cdef NAgent *a
        while (not self.q.empty()) and self.now and self.now <= self.stop_time:
            ev = self.q.pop_ev()
            kind = ev.kind
            sid = ev.sender_id
            rid = ev.recipient_id
            mid = ev.message_id
            self.now = ev.deliver_at
            if kind == KIND_WAKE:
                busy = self.atime[rid]
                if busy > self.now:
                    ev.deliver_at = busy
                    self.q._push_raw(ev)
                    continue
                self.atime[rid] = self.now
                self.causal = mid
                self.ledger.append_c(
                    mid, rid, rid, 0, self.now, 0, 0, 0, 0, <int>self.seq,
                    LF_SEND_NA | LF_OID_NA | LF_PARENT_NA,
                )
                self.seq += 1
                self._sched_wake(&self.agents[rid])
                self.atime[rid] += self.cdelay[rid]
                continue
            busy = self.atime[rid]
            if busy > self.now:
                ev.deliver_at = busy
                self.q._push_raw(ev)
                continue
            self.atime[rid] = self.now
            self.atime[rid] += self.cdelay[rid]
            self.causal = mid
            self._deliver_seq(mid, rid)
            if kind == KIND_LIMIT or kind == KIND_CANCEL_REQ or kind == KIND_SPREAD_REQ or kind == KIND_HOURS_REQ or kind == KIND_CLOSE_REQ:
                self._exch_recv(sid, &ev)
            else:
                self._agent_recv(&self.agents[rid], &ev)

    def result(self):
        return self.trace.to_dataframe(), self.ledger.to_dataframe()


def run_native_sim(spec):
    cdef NativeSim sim = NativeSim()
    sim.setup(spec)
    sim.run_loop()
    return sim.result()
