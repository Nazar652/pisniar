"""Celery worker: task execution and the shared Celery app configuration.

Does not import anything from `pisniar.bot` — there is no `bot ↔ worker`
edge. Both `bot` (as a task producer) and `worker` (as the consumer) build a
Celery client from `pisniar.worker.celery_app.build_celery_app`, and both
send their own Telegram alerts via `pisniar.telegram.notifier` directly.
"""
