"""Columnar trace + message ledger — no per-row dict or 10-tuple.

Exchange-internal hops write primitives here; extract wraps the columns
once. Agent-facing Python ``Message`` / ``LimitOrder`` objects are not
stored in these buffers.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from abides_fork.trace import _MSG_DTYPES, _TRACE_DTYPES


class ColumnTrace:
    """Order + quote columns written at the source, not walked from ``.log``."""

    __slots__ = (
        "ot",
        "oev",
        "oaid",
        "oside",
        "opx",
        "osz",
        "ooid",
        "qt",
        "qaid",
        "qside",
        "qpx",
        "qsz",
    )

    def __init__(self) -> None:
        self.ot: list[int] = []
        self.oev: list[str] = []
        self.oaid: list[int] = []
        self.oside: list[Any] = []
        self.opx: list[int] = []
        self.osz: list[int] = []
        self.ooid: list[int] = []
        self.qt: list[int] = []
        self.qaid: list[int] = []
        self.qside: list[str] = []
        self.qpx: list[int] = []
        self.qsz: list[int] = []

    def __bool__(self) -> bool:
        return bool(self.ot or self.qt)

    def add_order(
        self,
        t_ns: int,
        event_type: str,
        agent_id: int,
        side: Any,
        price: int,
        size: int,
        order_id: int,
    ) -> None:
        self.ot.append(t_ns)
        self.oev.append(event_type)
        self.oaid.append(agent_id)
        self.oside.append(side)
        self.opx.append(price)
        self.osz.append(size)
        self.ooid.append(order_id)

    def add_quote(
        self, t_ns: int, is_bid: bool, price: int, size: int, agent_id: int
    ) -> None:
        self.qt.append(t_ns)
        self.qaid.append(agent_id)
        self.qside.append("BID" if is_bid else "ASK")
        self.qpx.append(price)
        self.qsz.append(size)

    def to_dataframe(self) -> pd.DataFrame:
        from fast_sim.extract import (
            _ORDER_EVENT_MAP,
            _empty_trace,
            _side_to_str,
            _stable_lexsort,
        )

        if not self.ot and not self.qt:
            return _empty_trace()

        n_order = len(self.ot)
        if n_order:
            t_arr = np.fromiter(self.ot, dtype=np.int64, count=n_order)
            aid_arr = np.fromiter(self.oaid, dtype=np.int64, count=n_order)
            oid_arr = np.fromiter(self.ooid, dtype=np.int64, count=n_order)
            px_arr = np.fromiter(self.opx, dtype=np.int64, count=n_order)
            sz_arr = np.fromiter(self.osz, dtype=np.int64, count=n_order)
            ev_arr = np.array(self.oev, dtype=object)
            side_raw = np.array(self.oside, dtype=object)

            order_idx = np.argsort(t_arr, kind="stable")
            t_arr = t_arr[order_idx]
            aid_arr = aid_arr[order_idx]
            oid_arr = oid_arr[order_idx]
            px_arr = px_arr[order_idx]
            sz_arr = sz_arr[order_idx]
            ev_arr = ev_arr[order_idx]
            side_raw = side_raw[order_idx]

            is_exec = ev_arr == "ORDER_EXECUTED"
            last_exec: dict[int, int] = {}
            for pos in np.nonzero(is_exec)[0]:
                last_exec[int(oid_arr[pos])] = int(pos)
            msg = np.empty(n_order, dtype=object)
            for i, ev in enumerate(ev_arr):
                if ev == "ORDER_EXECUTED":
                    msg[i] = (
                        "ORDER_FILLED"
                        if last_exec.get(int(oid_arr[i])) == i
                        else "PARTIAL_FILL"
                    )
                else:
                    msg[i] = _ORDER_EVENT_MAP[ev]
            uniq = {v: _side_to_str(v) for v in set(self.oside) if v is not None}
            side_str = np.array([uniq.get(v) for v in side_raw], dtype=object)
        else:
            t_arr = aid_arr = oid_arr = px_arr = sz_arr = msg = side_str = None

        n_quote = 0
        if self.qt:
            last_i: dict[tuple[int, str], int] = {}
            first_rank: dict[tuple[int, str], int] = {}
            rank = 0
            for i, (qt, qs) in enumerate(zip(self.qt, self.qside)):
                key = (qt, qs)
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
                q_side[j] = self.qside[i]
                q_px[j] = self.qpx[i]
                q_sz[j] = self.qsz[i]
                q_msg[j] = "QUOTE_UPDATE"

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


class ColumnLedger:
    """Delivered-message columns. ``seq == -1`` means not yet delivered."""

    __slots__ = (
        "mid",
        "src",
        "dst",
        "t_send",
        "t_recv",
        "lat",
        "mtype",
        "oid",
        "parent",
        "seq",
    )

    def __init__(self) -> None:
        self.mid: list[int] = []
        self.src: list[int] = []
        self.dst: list[int] = []
        self.t_send: list[Any] = []
        self.t_recv: list[int] = []
        self.lat: list[int] = []
        self.mtype: list[str] = []
        self.oid: list[Any] = []
        self.parent: list[Any] = []
        self.seq: list[int] = []

    def append(
        self,
        mid: int,
        src: int,
        dst: int,
        t_send: Any,
        t_recv: int,
        lat: int,
        mtype: str,
        oid: Any,
        parent: Any,
        seq: int = -1,
    ) -> int:
        self.mid.append(mid)
        self.src.append(src)
        self.dst.append(dst)
        self.t_send.append(t_send)
        self.t_recv.append(t_recv)
        self.lat.append(lat)
        self.mtype.append(mtype)
        self.oid.append(oid)
        self.parent.append(parent)
        self.seq.append(seq)
        return len(self.seq) - 1

    def set_seq(self, idx: int, seq: int) -> None:
        self.seq[idx] = seq

    def to_dataframe(self) -> pd.DataFrame:
        from fast_sim.extract import _empty_msg, _nullable_int64

        n = len(self.seq)
        if n == 0:
            return _empty_msg()
        seq = np.fromiter(self.seq, dtype=np.int64, count=n)
        keep = seq >= 0
        if not keep.any():
            return _empty_msg()
        order = np.argsort(seq[keep], kind="stable")
        # Compact kept rows then apply seq order.
        mid = np.fromiter(self.mid, dtype=np.int64, count=n)[keep][order]
        src = np.fromiter(self.src, dtype=np.int32, count=n)[keep][order]
        dst = np.fromiter(self.dst, dtype=np.int32, count=n)[keep][order]
        t_recv = np.fromiter(self.t_recv, dtype=np.int64, count=n)[keep][order]
        lat = np.fromiter(self.lat, dtype=np.int64, count=n)[keep][order]
        mtype = np.array(self.mtype, dtype=object)[keep][order]
        seq_out = seq[keep][order]

        t_send = np.empty(n, dtype=np.int64)
        t_send_na = np.zeros(n, dtype=np.bool_)
        oid = np.empty(n, dtype=np.int64)
        oid_na = np.zeros(n, dtype=np.bool_)
        parent = np.empty(n, dtype=np.int64)
        parent_na = np.zeros(n, dtype=np.bool_)
        for i in range(n):
            ts = self.t_send[i]
            if ts is None:
                t_send_na[i] = True
            else:
                t_send[i] = ts
            o = self.oid[i]
            if o is None:
                oid_na[i] = True
            else:
                oid[i] = o
            p = self.parent[i]
            if p is None:
                parent_na[i] = True
            else:
                parent[i] = p
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
