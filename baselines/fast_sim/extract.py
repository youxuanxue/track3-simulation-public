"""Build canonical traces from in-memory agent logs — no parse_logs_df.

``parse_logs_df`` concatenates every agent's full log (including HOLDINGS_UPDATED
and depth dumps) into one pandas frame. After ``optimize.apply_runtime_patches``
the logs already contain only the event types that become ``trace.parquet``
rows; this module walks those lists and applies the same ORDER_FILLED /
QUOTE_UPDATE rules as ``abides_fork.trace.extract_trace``.
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
    extract_message_trace,
)

_ORDER_EVENT_MAP: dict[str, str] = {
    "ORDER_SUBMITTED": "ORDER_SUBMITTED",
    "ORDER_ACCEPTED": "ORDER_ACCEPTED",
    "ORDER_CANCELLED": "ORDER_CANCELLED",
    "ORDER_REPLACED": "ORDER_REPLACED",
}


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


def extract_trace_from_agents(agents: list[Any]) -> pd.DataFrame:
    """Canonical 7-column trace from agent ``.log`` lists."""
    t_ns: list[int] = []
    agent_ids: list[int] = []
    event_types: list[str] = []
    sides: list[Any] = []
    prices: list[Any] = []
    sizes: list[Any] = []
    order_ids: list[Any] = []
    quote_payloads: list[tuple[int, int, str, int, int]] = []

    for agent in agents:
        aid = int(agent.id)
        for event_time, event_type, event in agent.log:
            if event_type in _ORDER_EVENT_MAP or event_type == "ORDER_EXECUTED":
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
                    prices.append(event.get("fill_price"))
                else:
                    prices.append(event.get("limit_price"))
                sizes.append(event.get("quantity"))
                order_ids.append(int(oid))
            elif event_type in ("BEST_BID", "BEST_ASK"):
                text = str(event)
                parts = text.split(",")
                if len(parts) != 3:
                    continue
                try:
                    q_price = int(parts[1])
                    q_size = int(parts[2])
                except (TypeError, ValueError):
                    continue
                side = "BID" if event_type == "BEST_BID" else "ASK"
                quote_payloads.append(
                    (int(event_time), int(aid), side, q_price, q_size)
                )

    if t_ns:
        o = pd.DataFrame(
            {
                "t_ns": t_ns,
                "agent_id": agent_ids,
                "EventType": event_types,
                "side": sides,
                "price": prices,
                "size": sizes,
                "order_id": order_ids,
            }
        )
        o = o.sort_values("t_ns", kind="stable").reset_index(drop=True)
        is_exec = o["EventType"] == "ORDER_EXECUTED"
        final_pos = o[is_exec].drop_duplicates("order_id", keep="last").index
        is_final = o.index.isin(final_pos)
        msg_type = (
            o["EventType"]
            .map(_ORDER_EVENT_MAP)
            .where(
                ~is_exec,
                pd.Series(
                    np.where(is_final, "ORDER_FILLED", "PARTIAL_FILL"), index=o.index
                ),
            )
        )
        side_map = {v: _side_to_str(v) for v in o["side"].dropna().unique()}
        order_df = pd.DataFrame(
            {
                "t_ns": o["t_ns"],
                "agent_id": o["agent_id"],
                "msg_type": msg_type,
                "side": o["side"].map(side_map),
                "price": o["price"],
                "size": o["size"],
                "order_id": o["order_id"],
            }
        )
    else:
        order_df = pd.DataFrame(columns=TRACE_COLUMNS)

    if quote_payloads:
        qv = pd.DataFrame(
            quote_payloads, columns=["t_ns", "agent_id", "side", "price", "size"]
        )
        key = qv["t_ns"].astype("int64").astype(str) + "|" + qv["side"]
        quote_df = pd.DataFrame(
            {
                "t_ns": qv["t_ns"],
                "agent_id": qv["agent_id"],
                "msg_type": "QUOTE_UPDATE",
                "side": qv["side"],
                "price": qv["price"],
                "size": qv["size"],
                "order_id": -1,
                "_rank": pd.factorize(key)[0],
            }
        )
        quote_df = (
            quote_df.drop_duplicates(["t_ns", "side"], keep="last")
            .sort_values("_rank", kind="stable")
            .drop(columns="_rank")
        )
    else:
        quote_df = pd.DataFrame(columns=TRACE_COLUMNS)

    trace = pd.concat([order_df, quote_df], ignore_index=True)
    if trace.empty:
        return trace.astype(_TRACE_DTYPES)
    trace["price"] = trace["price"].fillna(0)
    trace["size"] = trace["size"].fillna(0)
    trace["order_id"] = trace["order_id"].fillna(-1)
    trace = trace.sort_values(["t_ns", "order_id"], kind="stable").reset_index(
        drop=True
    )
    return trace.astype(_TRACE_DTYPES)


def extract_message_trace_from_state(end_state: dict[str, Any]) -> pd.DataFrame:
    """Delegate to the adapter extractor (ledger already lives on end_state)."""
    df = extract_message_trace(end_state)
    if df.empty:
        return pd.DataFrame({c: [] for c in MESSAGE_TRACE_COLUMNS}).astype(_MSG_DTYPES)
    return df
