"""The worker loop: reconciler, scheduler, validation and outbox delivery in one process."""
from __future__ import annotations

import logging
import threading

from cas.adapters.base import load_adapter_configs
from cas.adapters.local import InProcessAdapter, LocalProcessAdapter
from cas.config import settings
from cas.persistence.db import transaction
from cas.service import notifications
from cas.workers import reconciler, validation
from cas.workers.scheduler import Scheduler

log = logging.getLogger("cas.worker")


def default_scheduler() -> Scheduler:
    return Scheduler({"local_process": LocalProcessAdapter(), "in_process": InProcessAdapter()}, load_adapter_configs())


def tick(scheduler: Scheduler | None = None, tenant_id: str | None = None) -> dict:
    """One pass of every worker. Safe to call repeatedly and from several processes."""
    stats = {"reconciler": reconciler.run_once(tenant_id)}
    if scheduler is not None:
        stats["assigned"] = scheduler.assign_once(tenant_id)
        stats["runs_driven"] = scheduler.drive_once(tenant_id)
    stats["validation"] = validation.run_once(tenant_id)
    stats["outbox"] = notifications.dispatch_pending()
    return stats


def purge_idempotency() -> int:
    with transaction() as tx:
        return tx.execute("DELETE FROM idempotency_records WHERE created_at < now() - make_interval(hours => %s)",
                          (settings.idempotency_retention_hours,)).rowcount


def run_forever(stop: threading.Event | None = None, scheduler: Scheduler | None = None) -> None:
    stop = stop or threading.Event()
    scheduler = scheduler or default_scheduler()
    ticks = 0
    while not stop.is_set():
        try:
            tick(scheduler)
            ticks += 1
            if ticks % 600 == 0:
                purge_idempotency()
        except Exception:  # noqa: BLE001 - keep the loop alive; the next tick retries
            log.exception("worker tick failed")
        stop.wait(settings.worker_tick_seconds)
