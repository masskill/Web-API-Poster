"""Delayed publications: APScheduler wakes the worker at the right time.

Jobs themselves live in the DB, so on startup restore() re-registers every future wake-up.
Also runs the automation tasks: daily login check, cleanup, pre-publication login check.
"""
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from sqlmodel import select

from app import settings_store
from app.db import get_session
from app.models import Job, utcnow
from app.posting import local_tz

PREFLIGHT_BEFORE = timedelta(minutes=30)


class Scheduler:
    def __init__(self, wake, preflight=None):
        self.wake = wake
        self.preflight = preflight  # called with run_at some time before scheduled jobs
        self.aps = BackgroundScheduler(timezone="UTC")

    def start(self) -> int:
        self.aps.start()
        return self.restore()

    def shutdown(self) -> None:
        if self.aps.running:
            self.aps.shutdown(wait=False)

    def schedule(self, run_at: datetime) -> None:
        """run_at is an aware datetime (UTC, as stored in the DB)."""
        if run_at <= utcnow():
            self.wake()
            return
        when = run_at.astimezone(timezone.utc)
        self.aps.add_job(self.wake, DateTrigger(run_date=when), id=f"wake-{int(when.timestamp())}",
                         replace_existing=True, misfire_grace_time=None)
        if self.preflight and when - PREFLIGHT_BEFORE > utcnow():
            self.aps.add_job(self.preflight, DateTrigger(run_date=when - PREFLIGHT_BEFORE), args=[run_at],
                             id=f"preflight-{int(when.timestamp())}", replace_existing=True)

    def restore(self) -> int:
        """Re-register wake-ups for pending jobs; returns how many future wake-ups were set."""
        with get_session() as s:
            times = set(s.exec(select(Job.run_at).where(Job.status == "pending")).all())
        future = [t for t in times if t > utcnow()]
        for t in future:
            self.schedule(t)
        if len(future) < len(times):
            self.wake()
        return len(future)

    def start_automation(self, login_check, cleanup, inbox=None) -> None:
        """(Re)register the recurring automation tasks; called at startup and after settings change."""
        tz = local_tz()
        check_time = settings_store.get("login_check_time").strip()
        if self.aps.get_job("login-check"):
            self.aps.remove_job("login-check")
        try:
            hour, minute = (int(x) for x in check_time.split(":"))
            self.aps.add_job(login_check, CronTrigger(hour=hour, minute=minute, timezone=tz), id="login-check")
        except ValueError:
            pass  # empty or invalid time: daily check is off
        self.aps.add_job(cleanup, CronTrigger(hour=3, minute=30, timezone=tz), id="cleanup", replace_existing=True)
        if inbox:
            self.aps.add_job(inbox, "interval", seconds=60, id="inbox", replace_existing=True,
                             max_instances=1, coalesce=True)
