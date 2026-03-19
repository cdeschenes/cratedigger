"""
webapp/scheduler.py — optional cron-style scheduling for both jobs.

Environment variables (all optional, empty = disabled):
  SCHEDULE_MISSING   — 5-field cron expression, e.g. "0 3 * * 0"  (Sunday 3am)
  SCHEDULE_DISCOVER  — 5-field cron expression, e.g. "0 4 * * 0"  (Sunday 4am)
"""
import logging
import os

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from webapp.runner import run_job

log = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


def _parse_cron(expr: str) -> CronTrigger | None:
    expr = expr.strip()
    if not expr:
        return None
    fields = expr.split()
    if len(fields) != 5:
        log.warning("Invalid cron expression (need 5 fields): %r", expr)
        return None
    minute, hour, day, month, day_of_week = fields
    try:
        return CronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=day_of_week,
        )
    except Exception as exc:
        log.warning("Could not parse cron expression %r: %s", expr, exc)
        return None


async def _run_missing() -> None:
    try:
        await run_job("missing")
    except RuntimeError:
        log.info("Scheduled run of 'missing' skipped — already running.")


async def _run_discover() -> None:
    try:
        await run_job("discover")
    except RuntimeError:
        log.info("Scheduled run of 'discover' skipped — already running.")


def get_next_run(job_id: str) -> str | None:
    """Return ISO next-run time for a scheduled job, or None if not scheduled."""
    if _scheduler is None:
        return None
    sched_id = f"{job_id}_scheduled"
    job = _scheduler.get_job(sched_id)
    if job is None or job.next_run_time is None:
        return None
    return job.next_run_time.isoformat()


def start_scheduler() -> None:
    global _scheduler
    _scheduler = AsyncIOScheduler()

    if trigger := _parse_cron(os.environ.get("SCHEDULE_MISSING", "")):
        _scheduler.add_job(_run_missing, trigger, id="missing_scheduled")
        log.info("Scheduled missing_popular_albums: %s", os.environ.get("SCHEDULE_MISSING", ""))

    if trigger := _parse_cron(os.environ.get("SCHEDULE_DISCOVER", "")):
        _scheduler.add_job(_run_discover, trigger, id="discover_scheduled")
        log.info("Scheduled discover_similar_artists: %s", os.environ.get("SCHEDULE_DISCOVER", ""))

    _scheduler.start()
    log.info("APScheduler started.")


def stop_scheduler() -> None:
    if _scheduler:
        _scheduler.shutdown(wait=False)
        log.info("APScheduler stopped.")
