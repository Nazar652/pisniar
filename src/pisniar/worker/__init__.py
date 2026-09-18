"""Celery worker: task execution and the shared Celery app configuration.

Does not import from `pisniar.bot` — no `bot ↔ worker` edge. Both build a
Celery client from `celery_app.build_celery_app` and send alerts directly.
"""
