import asyncio
from datetime import timedelta

from app import settings_store
from app.db import get_session
from app.models import Account, Job, Post, utcnow
from app.publishers.base import PublishResult
from app.scheduler import Scheduler
from app.texts import PlatformText
from app.worker import (JobLog, Worker, cancel_job, daily_limit_wait, interrupt_stale_jobs, next_due_job_id,
                        retry_job)


class FakePublisher:
    platform = "fake"
    uses_browser = False
    calls = []
    outcome = PublishResult("published", url="https://example.com/p/1")

    def __init__(self, account, log):
        self.log = log

    async def is_logged_in(self, page):
        return True

    async def publish(self, page, video, text, dry_run):
        self.log("publishing")
        FakePublisher.calls.append((text.description, dry_run))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def make_job(tmp_path, run_at=None, dry_run=False):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    with get_session() as s:
        acc = Account(platform="fake", name="A", slug="a")
        post = Post(video_path=str(video), dry_run=dry_run)
        s.add(acc)
        s.add(post)
        s.commit()
        job = Job(post_id=post.id, account_id=acc.id, run_at=run_at or utcnow(), dry_run=dry_run,
                  text_json=PlatformText(description="hi").to_json())
        s.add(job)
        s.commit()
        return job.id


def get_job(job_id):
    with get_session() as s:
        return s.get(Job, job_id)


def run_job(job_id, outcome):
    FakePublisher.calls = []
    FakePublisher.outcome = outcome
    asyncio.run(Worker({"fake": FakePublisher}).run_job(job_id))
    return get_job(job_id)


def test_successful_job(tmp_path):
    job_id = make_job(tmp_path)
    assert next_due_job_id() == job_id
    job = run_job(job_id, PublishResult("published", url="https://example.com/p/1"))
    assert job.status == "published" and job.result_url == "https://example.com/p/1"
    assert "publishing" in job.log and job.attempts == 1
    assert FakePublisher.calls == [("hi", False)]
    assert next_due_job_id() is None


def test_dry_run_status(tmp_path):
    job = run_job(make_job(tmp_path, dry_run=True), PublishResult("dry_run"))
    assert job.status == "dry_run" and FakePublisher.calls == [("hi", True)]


def test_exception_marks_failed_with_debug(tmp_path):
    job = run_job(make_job(tmp_path), RuntimeError("boom"))
    assert job.status == "failed" and "boom" in job.error
    assert job.debug_dir
    assert "boom" in open(f"{job.debug_dir}/steps.log", encoding="utf-8").read()


def test_retry_and_cancel(tmp_path):
    job_id = run_job(make_job(tmp_path), RuntimeError("boom")).id
    assert retry_job(job_id)
    assert get_job(job_id).status == "pending" and get_job(job_id).error is None
    assert cancel_job(job_id)
    assert get_job(job_id).status == "cancelled"
    assert next_due_job_id() is None
    assert not cancel_job(job_id)


def test_future_job_not_due(tmp_path):
    make_job(tmp_path, run_at=utcnow() + timedelta(hours=1))
    assert next_due_job_id() is None


def test_daily_limit_postpones(tmp_path):
    settings_store.set_value("daily_limit", "1")
    first = run_job(make_job(tmp_path), PublishResult("published"))
    assert daily_limit_wait(first.account_id) is not None
    with get_session() as s:
        second = Job(post_id=first.post_id, account_id=first.account_id, text_json="{}")
        s.add(second)
        s.commit()
        second_id = second.id
    job = run_job(second_id, PublishResult("published"))
    assert job.status == "pending" and job.error_code == "daily_limit"
    assert job.run_at > utcnow() + timedelta(hours=23)
    assert FakePublisher.calls == []


def test_interrupted_jobs_after_restart(tmp_path):
    job_id = make_job(tmp_path)
    with get_session() as s:
        job = s.get(Job, job_id)
        job.status = "running"
        s.add(job)
        s.commit()
    assert interrupt_stale_jobs() == 1
    assert get_job(job_id).status == "failed" and get_job(job_id).error_code == "interrupted"


def test_scheduler_restores_future_jobs(tmp_path):
    wakes = []
    make_job(tmp_path, run_at=utcnow() + timedelta(hours=2))
    make_job(tmp_path, run_at=utcnow() + timedelta(hours=3))
    make_job(tmp_path)  # already due
    sched = Scheduler(lambda: wakes.append(1))
    try:
        assert sched.start() == 2
        assert len(sched.aps.get_jobs()) == 2
        assert wakes == [1]  # due job wakes the worker immediately
    finally:
        sched.shutdown()


def test_job_log_appends(tmp_path):
    job_id = make_job(tmp_path)
    log = JobLog(job_id)
    log("one")
    log("two")
    assert get_job(job_id).log.count("\n") == 1 and len(log.lines) == 2
