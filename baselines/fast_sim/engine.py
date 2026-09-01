"""Run one scenario through patched ABIDES and return traces + kernel end_state."""

from __future__ import annotations

from typing import Any

import numpy as np
from abides_core.kernel import Kernel
from abides_core.message import Message
from abides_core.utils import subdict
from abides_markets.orders import Order

from abides_fork.config import build_config
from fast_sim.extract import extract_message_trace_from_state, extract_trace_from_agents
from fast_sim.optimize import HeapPQueue, apply_runtime_patches, slim_agents, slim_exchange

try:
    from fast_sim._hotpath import EventQueue
except ImportError:
    try:
        from fast_sim.hotpath import EventQueue
    except ImportError:
        EventQueue = HeapPQueue


def reset_abides_counters() -> None:
    """Reset ABIDES class-level id counters so a run is deterministic in-process."""
    Order._order_id_counter = 0
    setattr(Message, "_Message__message_id_counter", 1)


def run_scenario(scenario: dict[str, Any]) -> tuple[Any, Any, dict[str, Any]]:
    """Execute ``scenario`` and return ``(trace_df, message_trace_df, end_state)``.

    The kernel, matching engine, agents, oracle and latency model are the pinned
    ABIDES stack. The event queue is a compact C min-heap (Python heapq fallback)
    with the same comparison key ABIDES uses:
    ``(deliver_at, (sender_id, recipient_id, message))`` / ``Message.__lt__``
    by ``message_id``. Delivery order is unchanged.
    """
    apply_runtime_patches()
    reset_abides_counters()

    config = build_config(scenario)
    agents = config["agents"]
    slim_exchange(agents[0])
    slim_agents(agents)

    # abides_core.abides.run ignores config["random_state_kernel"] and constructs
    # Kernel(random_state=RandomState(seed=0)). Match that exactly so any latent
    # kernel RNG use (legacy latency-noise path) stays aligned with the references.
    kernel = Kernel(
        random_state=np.random.RandomState(seed=0),
        log_dir="",
        skip_log=True,
        **subdict(
            config,
            [
                "start_time",
                "stop_time",
                "agents",
                "agent_latency_model",
                "default_computation_delay",
                "custom_properties",
            ],
        ),
    )
    kernel.messages = EventQueue()
    kernel.show_trace_messages = False
    # Populated at deliver time so extract can skip the ledger×seq join+sort.
    kernel._delivered = []
    kernel._pending_ledger = {}

    end_state = kernel.run()
    # ExchangeAgent.kernel_terminating is a no-op; surface the ledger the
    # kernel_message_ledger patch already wrote onto custom_state.
    if "message_ledger" not in end_state and hasattr(kernel, "_msg_ledger"):
        end_state["message_ledger"] = kernel._msg_ledger
        end_state["deliver_seq_by_key"] = kernel._deliver_seq_by_key
    if hasattr(kernel, "_delivered"):
        end_state["delivered_ledger"] = kernel._delivered
    if "agents" not in end_state:
        end_state["agents"] = agents

    trace = extract_trace_from_agents(agents)
    message_trace = extract_message_trace_from_state(end_state)
    return trace, message_trace, end_state
