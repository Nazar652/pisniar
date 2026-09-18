"""Bridge between Celery's synchronous task protocol and async application code.

asyncpg connections cannot cross event loops, and Celery tasks are plain
synchronous callables — so each task creates its own event loop via
`asyncio.run()`. Combined with a `NullPool` engine built fresh per task (see
`pisniar.db.session.build_engine(..., use_null_pool=True)`) this gives every
task its own connection, opened and closed within the task's own loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine


def run_async[T](coro: Coroutine[object, object, T]) -> T:
    return asyncio.run(coro)
