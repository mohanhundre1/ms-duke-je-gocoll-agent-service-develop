"""A2A agent layer - discovery card export.

The A2A executor seam was removed in Phase 3: the agent now runs as the
framework ``PipelineExecutor``: the agent now runs as the
(wired in ``src.__main__`` wrapping :class:`src.logic.agent.GoCollAgentLogic`
card is exported here.
"""
from src.agents.agent_card import create_agent_card

__all__ = ["create_agent_card"]

