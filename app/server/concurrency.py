from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TypeVar


T = TypeVar("T")


async def run_sync_cancellation_safe(
    func: Callable[..., T],
    /,
    *args,
    **kwargs,
) -> T:
    """Run blocking work in a context-propagating thread and drain it on cancellation.

    Feature admission must not be released merely because the client disconnected
    while pyodbc/SQLAlchemy work is still running in the worker thread.
    """

    task = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except Exception:
            pass
        raise
