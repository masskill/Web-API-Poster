"""Delayed publications: APScheduler wakes the worker at the right time.

Jobs themselves live in the DB, so on startup restore() re-registers every future wake-up.
"""
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from sqlmodel import select

from app.db import get_session
from app.models import Job, utcnow


class Scheduler:
    def __init__(self, wake):
        self.wake = wake
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
