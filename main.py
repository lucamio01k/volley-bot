"""Long-running scheduler entry point for the Italia Volley Telegram bot."""

from __future__ import annotations

import signal
import sys
import time

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import config
import database
from logger import get_logger
from tasks import (
    sync_schedule,
    task_send_daily,
    task_send_reminders,
    task_send_results,
    task_send_weekly,
    task_telegram_health,
)
from telegram_sender import configured

log = get_logger(__name__)


def setup_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="UTC")
    source = config.section("source_refresh")
    reminder = config.section("match_reminder")
    result = config.section("result_notification")
    weekly = config.section("weekly_digest")
    daily = config.section("daily_digest")

    scheduler.add_job(
        sync_schedule,
        IntervalTrigger(minutes=max(1, int(source.get("active_interval_minutes", 15))), timezone="UTC"),
        id="schedule_sync",
        name="CEV schedule sync",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=900,
    )
    scheduler.add_job(
        task_send_reminders,
        IntervalTrigger(minutes=max(1, int(reminder.get("check_interval_minutes", 10))), timezone="UTC"),
        id="match_reminders",
        name="Upcoming match reminders",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=900,
    )
    scheduler.add_job(
        task_send_results,
        IntervalTrigger(minutes=max(1, int(result.get("check_interval_minutes", 10))), timezone="UTC"),
        id="match_results",
        name="Final result polling",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=900,
    )
    scheduler.add_job(
        task_telegram_health,
        IntervalTrigger(minutes=15, timezone="UTC"),
        id="telegram_health",
        name="Telegram health",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        task_send_weekly,
        CronTrigger(
            day_of_week=str(weekly.get("day", "mon")),
            hour=int(weekly.get("hour", 9)),
            minute=int(weekly.get("minute", 0)),
            timezone=config.BOT_TIMEZONE,
        ),
        id="weekly_digest",
        name="Weekly Italy schedule",
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        task_send_daily,
        CronTrigger(
            hour=int(daily.get("hour", 8)),
            minute=int(daily.get("minute", 30)),
            timezone=config.BOT_TIMEZONE,
        ),
        id="daily_digest",
        name="Match-day digest",
        misfire_grace_time=3600,
    )
    return scheduler


def _run_startup_tasks() -> None:
    task_telegram_health()
    # Existing notification dedupe keys make startup sync safe. Keeping
    # notifications enabled avoids losing a fixture/change discovered exactly
    # during a service restart.
    sync_schedule(force=True, notify=True)


def main() -> None:
    database.init_db(config.DB_PATH)
    if not configured():
        log.error("Telegram credentials are missing; configure .env before starting the service")
        raise SystemExit(1)

    log.info("Starting Italia Volley bot")
    _run_startup_tasks()

    scheduler = setup_scheduler()
    scheduler.start()
    for job in scheduler.get_jobs():
        log.info("Scheduled [%s] %s; next run %s", job.id, job.name, job.next_run_time)

    stopping = False

    def stop(signum, _frame) -> None:
        nonlocal stopping
        log.info("Shutdown signal received: %s", signum)
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not stopping:
            time.sleep(1)
    finally:
        scheduler.shutdown(wait=True)
        log.info("Italia Volley bot stopped")


if __name__ == "__main__":
    main()
