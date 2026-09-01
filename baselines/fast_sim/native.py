"""From-scratch C kernel entry. Hybrid ``Kernel.run`` stays the fallback.

``native_supported`` is true when the roster is Exchange + the four Track-3
scheduled agents. ``run_native`` snapshots params / RNG streams from the
ABIDES objects ``build_config`` already constructed (so seed order matches
the references) and then runs a C event loop that never calls ``Kernel.run``.
"""

from __future__ import annotations

import os
from typing import Any

# Wired as default after s001 120/84/74/0/604/664 matched bit-exact.
NATIVE_IS_DEFAULT = True

_SUPPORTED = {"ExchangeAgent", "NoiseTrader", "MarketMaker", "ValueTrader", "MomentumTrader"}


def native_flag() -> str:
    return os.environ.get("FAST_SIM_NATIVE", "auto").strip().lower()


def native_supported(agents: list[Any]) -> bool:
    if not agents:
        return False
    if type(agents[0]).__name__ != "ExchangeAgent":
        return False
    for agent in agents[1:]:
        if type(agent).__name__ not in _SUPPORTED:
            return False
    return True


def should_use_native(agents: list[Any]) -> bool:
    flag = native_flag()
    if flag in ("0", "false", "hybrid", "off"):
        return False
    if not native_supported(agents):
        return False
    if flag in ("1", "true", "native", "on"):
        return True
    return NATIVE_IS_DEFAULT


def _kind(agent: Any) -> int:
    name = type(agent).__name__
    if name == "ExchangeAgent":
        return 0
    if name == "NoiseTrader":
        return 1
    if name == "MarketMaker":
        return 2
    if name == "ValueTrader":
        return 3
    if name == "MomentumTrader":
        return 4
    raise TypeError(f"unsupported agent {name}")


def _stp_code(policy: Any) -> int:
    if policy == "cancel_oldest":
        return 2
    if policy:
        return 1
    return 0


def _latency_spec(model: Any) -> dict[str, Any]:
    name = getattr(model, "_model", "deterministic")
    return {
        "model": str(name),
        "min_ns": float(getattr(model, "_min_ns", 0.0)),
        "max_ns": float(getattr(model, "_max_ns", 1e12)),
        "mean_ns": float(getattr(model, "_mean_ns", 0.0)),
        "sigma": float(getattr(model, "_sigma", 0.0)),
        "mu": float(getattr(model, "_mu", 0.0)),
        "alpha": float(getattr(model, "_alpha", 1.5)),
        "random_state": model.random_state,
    }


def snapshot_native(config: dict[str, Any]) -> dict[str, Any]:
    """Pull scalar fields + RNG objects off the ABIDES config (no Kernel)."""
    agents = config["agents"]
    exchange = agents[0]
    oracle = config.get("custom_properties", {}).get("oracle") or getattr(
        exchange, "oracle", None
    )
    mkt_open = int(exchange.mkt_open)
    mkt_close = int(exchange.mkt_close)
    last_trade = int(oracle.get_daily_open_price("ABM", mkt_open)) if oracle is not None else 100000
    roster = []
    for agent in agents:
        k = _kind(agent)
        row: dict[str, Any] = {
            "id": int(agent.id),
            "kind": k,
            "interval_ns": int(getattr(agent, "interval_ns", 0) or 0),
            "log_orders": bool(getattr(agent, "log_orders", True)),
            "random_state": getattr(agent, "random_state", None),
            "order_size_mean": float(getattr(agent, "order_size_mean", 0.0) or 0.0),
            "order_size_std": float(getattr(agent, "order_size_std", 0.0) or 0.0),
            "price_offset_ticks": int(getattr(agent, "price_offset_ticks", 0) or 0),
            "reference_price": int(getattr(agent, "reference_price", last_trade) or last_trade),
            "spread_ticks": int(getattr(agent, "spread_ticks", 2) or 2),
            "depth_levels": int(getattr(agent, "depth_levels", 1) or 1),
            "size_per_level": int(getattr(agent, "size_per_level", 1) or 1),
            "threshold_ticks": int(getattr(agent, "threshold_ticks", 0) or 0),
            "sigma_n": float(getattr(agent, "sigma_n", 0.0) or 0.0),
            "lookback": int(getattr(agent, "lookback", 1) or 1),
        }
        roster.append(row)
    return {
        "start_time": int(config["start_time"]),
        "stop_time": int(config["stop_time"]),
        "mkt_open": mkt_open,
        "mkt_close": mkt_close,
        "default_computation_delay": int(config.get("default_computation_delay", 50)),
        "exchange_computation_delay": int(getattr(exchange, "computation_delay", 0) or 0),
        "pipeline_delay": int(getattr(exchange, "pipeline_delay", 0) or 0),
        "stp": _stp_code(getattr(exchange, "stp_policy", None)),
        "last_trade": last_trade,
        "n_agents": len(agents),
        "agents": roster,
        "latency": _latency_spec(config["agent_latency_model"]),
        "oracle": oracle,
    }


def run_native(config: dict[str, Any]) -> tuple[Any, Any, dict[str, Any]]:
    from fast_sim._native import run_native_sim

    spec = snapshot_native(config)
    trace, msg = run_native_sim(spec)
    return trace, msg, {"col_trace": None, "col_ledger": None, "agents": config["agents"]}
