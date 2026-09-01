"""Build canonical traces from in-memory agent logs — no parse_logs_df.

``parse_logs_df`` concatenates every agent's full log (including HOLDINGS_UPDATED
and depth dumps) into one pandas frame. After ``optimize.apply_runtime_patches``
the logs already contain only the event types that become ``trace.parquet``
rows; this module walks those lists and applies the same ORDER_FILLED /
QUOTE_UPDATE rules as ``abides_fork.trace.extract_trace``.

Phase 3: build numpy columns instead of routing Python lists through pandas'
object-array inferrer (that path dominated gb_mega wall after Phase 2).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from abides_fork.trace import (
    MESSAGE_TRACE_COLUMNS,
    TRACE_COLUMNS,
    _MSG_DTYPES,
    _TRACE_DTYPES,
)

_ORDER_EVENT_MAP: dict[str, str] = {
    "ORDER_SUBMITTED": "ORDER_SUBMITTED",
    "ORDER_ACCEPTED": "ORDER_ACCEPTED",
    "ORDER_CANCELLED": "ORDER_CANCELLED",
    "ORDER_REPLACED": "ORDER_REPLACED",
}

_EMPTY_TRACE = None
_EMPTY_MSG = None


def _empty_trace() -> pd.DataFrame:
    global _EMPTY_TRACE
    if _EMPTY_TRACE is None:
        _EMPTY_TRACE = pd.DataFrame(columns=TRACE_COLUMNS).astype(_TRACE_DTYPES)
    return _EMPTY_TRACE.copy()


def _empty_msg() -> pd.DataFrame:
    global _EMPTY_MSG
    if _EMPTY_MSG is None:
        _EMPTY_MSG = pd.DataFrame({c: [] for c in MESSAGE_TRACE_COLUMNS}).astype(
            _MSG_DTYPES
        )
    return _EMPTY_MSG.copy()


def _side_to_str(value: Any) -> str | None:
    if value is None:
        return None
    enum_val = getattr(value, "value", None)
    if enum_val in ("BID", "ASK"):
        return str(enum_val)
    text = str(value).upper()
    if "BID" in text:
        return "BID"
    if "ASK" in text:
        return "ASK"
    return None


def _stable_lexsort(primary: np.ndarray, secondary: np.ndarray) -> np.ndarray:
    """Stable sort by ``primary`` then ``secondary`` (pandas ``kind='stable'``)."""
    idx = np.argsort(secondary, kind="stable")
    return idx[np.argsort(primary[idx], kind="stable")]


def extract_trace_from_agents(agents: list[Any]) -> pd.DataFrame:
    """Canonical 7-column trace from agent ``.log`` lists."""
    t_ns: list[int] = []
    agent_ids: list[int] = []
    event_types: list[str] = []
    sides: list[Any] = []
    prices: list[Any] = []
    sizes: list[Any] = []
    order_ids: list[int] = []
    quote_t: list[int] = []
    quote_aid: list[int] = []
    quote_side: list[str] = []
    quote_px: list[int] = []
    quote_sz: list[int] = []

    for agent in agents:
        aid = int(agent.id)
        for event_time, event_type, event in agent.log:
            if event_type == "ORDER_EXECUTED" or event_type in _ORDER_EVENT_MAP:
                if not isinstance(event, dict):
                    continue
                oid = event.get("order_id")
                if oid is None:
                    continue
                t_ns.append(int(event_time))
                agent_ids.append(int(event.get("agent_id", aid)))
                event_types.append(event_type)
                sides.append(event.get("side"))
                if event_type == "ORDER_EXECUTED":
                    fp = event.get("fill_price")
                    prices.append(0 if fp is None else int(fp))
                else:
                    lp = event.get("limit_price")
                    prices.append(0 if lp is None else int(lp))
                q = event.get("quantity")
                sizes.append(0 if q is None else int(q))
                order_ids.append(int(oid))
            elif event_type == "BEST_BID" or event_type == "BEST_ASK":
                text = event if isinstance(event, str) else str(event)
                parts = text.split(",")
                if len(parts) != 3:
                    continue
                try:
                    q_price = int(parts[1])
                    q_size = int(parts[2])
                except (TypeError, ValueError):
                    continue
                quote_t.append(int(event_time))
                quote_aid.append(int(aid))
                quote_side.append("BID" if event_type == "BEST_BID" else "ASK")
                quote_px.append(q_price)
                quote_sz.append(q_size)

    if t_ns:
        t_arr = np.fromiter(t_ns, dtype=np.int64, count=len(t_ns))
        aid_arr = np.fromiter(agent_ids, dtype=np.int64, count=len(agent_ids))
        oid_arr = np.fromiter(order_ids, dtype=np.int64, count=len(order_ids))
        px_arr = np.array(prices, dtype=np.int64)
        sz_arr = np.array(sizes, dtype=np.int64)
        ev_arr = np.array(event_types, dtype=object)
        side_raw = np.array(sides, dtype=object)

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
        exec_pos = np.nonzero(is_exec)[0]
        for pos in exec_pos:
            last_exec[int(oid_arr[pos])] = int(pos)
        msg = np.empty(len(ev_arr), dtype=object)
        for i, ev in enumerate(ev_arr):
            if ev == "ORDER_EXECUTED":
                msg[i] = (
                    "ORDER_FILLED"
                    if last_exec.get(int(oid_arr[i])) == i
                    else "PARTIAL_FILL"
                )
            else:
                msg[i] = _ORDER_EVENT_MAP[ev]

        uniq_sides = {v: _side_to_str(v) for v in set(sides) if v is not None}
        side_str = np.array(
            [uniq_sides.get(v) for v in side_raw], dtype=object
        )

        order_t = t_arr
        order_aid = aid_arr
        order_msg = msg
        order_side = side_str
        order_px = px_arr
        order_sz = sz_arr
        order_oid = oid_arr
        n_order = len(order_t)
    else:
        n_order = 0

    if quote_t:
        # Last quote per (t_ns, side); survivors ordered by first appearance of the key.
        last_i: dict[tuple[int, str], int] = {}
        first_rank: dict[tuple[int, str], int] = {}
        rank = 0
        for i, (qt, qs) in enumerate(zip(quote_t, quote_side)):
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
            q_t[j] = quote_t[i]
            q_aid[j] = quote_aid[i]
            q_side[j] = quote_side[i]
            q_px[j] = quote_px[i]
            q_sz[j] = quote_sz[i]
            q_msg[j] = "QUOTE_UPDATE"
    else:
        n_quote = 0

    if n_order == 0 and n_quote == 0:
        return _empty_trace()

    n = n_order + n_quote
    t_all = np.empty(n, dtype=np.int64)
    aid_all = np.empty(n, dtype=np.int64)
    msg_all = np.empty(n, dtype=object)
    side_all = np.empty(n, dtype=object)
    px_all = np.empty(n, dtype=np.int64)
    sz_all = np.empty(n, dtype=np.int64)
    oid_all = np.empty(n, dtype=np.int64)
    if n_order:
        t_all[:n_order] = order_t
        aid_all[:n_order] = order_aid
        msg_all[:n_order] = order_msg
        side_all[:n_order] = order_side
        px_all[:n_order] = order_px
        sz_all[:n_order] = order_sz
        oid_all[:n_order] = order_oid
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


def extract_message_trace_from_state(end_state: dict[str, Any]) -> pd.DataFrame:
    """Kernel ledger → ``message_trace.parquet``, without pandas object inference."""
    ledger = end_state.get("message_ledger") or []
    seqmap = end_state.get("deliver_seq_by_key") or {}
    if not ledger:
        return _empty_msg()

    get = seqmap.get
    pairs: list[tuple[int, dict]] = []
    for r in ledger:
        seq = get((int(r["message_id"]), int(r["dst_id"])))
        if seq is not None:
            pairs.append((seq, r))
    if not pairs:
        return _empty_msg()
    pairs.sort(key=lambda sr: sr[0])
    n = len(pairs)

    seq = np.empty(n, dtype=np.int64)
    t_recv = np.empty(n, dtype=np.int64)
    t_send = np.empty(n, dtype=np.int64)
    t_send_na = np.zeros(n, dtype=np.bool_)
    latency = np.empty(n, dtype=np.int64)
    src = np.empty(n, dtype=np.int32)
    dst = np.empty(n, dtype=np.int32)
    mid = np.empty(n, dtype=np.int64)
    msg_type = np.empty(n, dtype=object)
    oid = np.empty(n, dtype=np.int64)
    oid_na = np.zeros(n, dtype=np.bool_)
    parent = np.empty(n, dtype=np.int64)
    parent_na = np.zeros(n, dtype=np.bool_)

    for i, (s, r) in enumerate(pairs):
        seq[i] = s
        t_recv[i] = r["t_recv_ns"]
        ts = r["t_send_ns"]
        if ts is None:
            t_send_na[i] = True
        else:
            t_send[i] = ts
        latency[i] = r["latency_ns"]
        src[i] = r["src_id"]
        dst[i] = r["dst_id"]
        mid[i] = r["message_id"]
        msg_type[i] = r["msg_type"]
        o = r["order_id"]
        if o is None:
            oid_na[i] = True
        else:
            oid[i] = o
        p = r["causal_parent"]
        if p is None:
            parent_na[i] = True
        else:
            parent[i] = p

    def _nullable(values: np.ndarray, mask: np.ndarray) -> pd.Series:
        s = pd.array(values, dtype="Int64")
        if mask.any():
            s[mask] = pd.NA
        return s

    df = pd.DataFrame(
        {
            "seq": seq,
            "t_recv_ns": t_recv,
            "t_send_ns": _nullable(t_send, t_send_na),
            "latency_ns": latency,
            "src_id": src,
            "dst_id": dst,
            "message_id": mid,
            "msg_type": msg_type,
            "order_id": _nullable(oid, oid_na),
            "causal_parent": _nullable(parent, parent_na),
        }
    )
    return df.astype(_MSG_DTYPES, copy=False)
