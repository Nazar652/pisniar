"""Bridges Celery's sync task protocol to async code: each task gets its own
event loop, paired with a fresh `NullPool` engine (`db.session.build_engine`)
since asyncpg connections can't cross loops.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine


def run_async[T](coroutine: Coroutine[object, object, T]) -> T:
    return asyncio.run(coroutine)
