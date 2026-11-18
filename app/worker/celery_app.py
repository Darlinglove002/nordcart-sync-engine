from celery import Celery

from app.config import get_settings

_s = get_settings()

celery_app = Celery(
    "nordcart",
    broker=_s.celery_broker_url,
    backend=_s.celery_result_backend,
    include=["app.worker.tasks"],
)

celery_app.conf.update(
    task_always_eager=_s.celery_eager,
    task_eager_propagates=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    # A worker that dies mid-task hands the task back to the queue. Safe because every task
    # here is idempotent (rebuild-by-range, not increment).
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_soft_time_limit=900,
    task_time_limit=960,
    result_expires=3600,
    broker_connection_retry_on_startup=True,
    beat_schedule={
        "recalculate-dirty-sellers": {
            "task": "app.worker.tasks.recalculate_dirty",
            "schedule": float(_s.beat_interval_seconds),
        }
    },
)
