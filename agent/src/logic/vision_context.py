"""Request-scoped carrier for the governed VisionService ($8.5 / C6).

The ``agent_framework`` message bus can't cleanly carry a live service object
through the workflow DAG, so :class:`~src.logic.agent.GoCollAgentLogic` stashes
the governed ``ctx.services.vision`` in a context variable before running the
workflow, and the extraction node reads it back. Contextvars propagate into the
asyncio tasks the workflow spawns (each task snapshots the current context at
creation time), so the value bound here is visible inside
``GoCollExtractionExecutor.extract`` and the batch fan-out beneath it.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

_vision_service: ContextVar[Any | None] = ContextVar("gocoll_vision_service", default=None)


def get_current_vision_service() -> Any | None:
    """Return the governed VisionService bound to the current request, if any."""
    return _vision_service.get()


@contextmanager
def use_vision_service(service: Any | None) -> Iterator[None]:
    """Bind ``service`` as the request-scoped VisionService for the block."""
    token = _vision_service.set(service)
    try:
        yield
    finally:
        _vision_service.reset(token)