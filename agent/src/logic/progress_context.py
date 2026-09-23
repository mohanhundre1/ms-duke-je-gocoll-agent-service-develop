"""Request-scoped TPAO progress emitter contextvar (same pattern as vision_context).

GoCollAgentLogic binds an emit callback before running the workflow DAG, and
each workflow executor reads it back to emit real-time stage progress.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Awaitable, Callable, Iterator

_progress_cb: ContextVar[Callable[..., Awaitable[None]] | None] = ContextVar(
    "gocoll_progress_cb", default=None
)


async def emit_stage_progress(
    node: str,
    status: str,
    message: str,
    *,
    detail: str = "",
) -> None:
    """Emit a stage progress event; no-op when no callback is bound."""
    cb = _progress_cb.get()
    if cb is not None:
        if detail:
            await cb(node, status, message, detail)
        else:
            await cb(node, status, message)


@contextmanager
def use_progress_emitter(
    cb: Callable[..., Awaitable[None]] | None,
) -> Iterator[None]:
    """Bind ``cb`` as the request-scoped stage progress emitter for the block."""
    token = _progress_cb.set(cb)
    try:
        yield
    finally:
        _progress_cb.reset(token)