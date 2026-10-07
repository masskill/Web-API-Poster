"""Sequential job queue: one job (account x video) at a time, plus login / login-check actions."""
import asyncio
import logging
from datetime import timedelta
from pathlib import Path

from sqlmodel import func, select

from app import browser, settings_store
from app.db import get_session
from app.models import Account, Job, Post, utcnow
from app.publishers.base import NeedsUserAction, PublishResult
from app.texts import PlatformText

logger = logging.getLogger("poster.worker")

POLL_SECONDS = 30
MAX_USER_ROUNDS = 3

# Login button state per account id: queued | waiting | ok | timeout | error
login_state: dict[int, str] = {}


def default_publishers() -> dict:
    from app.publishers import PUBLISHERS
    return PUBLISHERS


class JobLog:
    """Step log of a job: kept in memory for debug files and appended to Job.log for the UI."""

    def __init__(self, job_id: int | None):
        self.job_id = job_id
        self.lines: list[str] = []

    def __call__(self, msg: str) -> None:
        line = f"[{utcnow():%H:%M:%S}] {msg}"
        self.lines.append(line)
        if self.job_id is None:
            return
        with get_session() as s:
            job = s.get(Job, self.job_id)
            if job:
                job.log = (job.log + "\n" + line).strip()
                s.add(job)
                s.commit()


# ---- queue state helpers (also used by the web UI) ----

def next_due_job_id() -> int | None:
    with get_session() as s:
        return s.exec(
            select(Job.id).where(Job.status == "pending", Job.run_at <= utcnow())
            .order_by(Job.run_at, Job.id)
        ).first()


def job_status(job_id: int) -> str | None:
    with get_session() as s:
        job = s.get(Job, job_id)
        return job.status if job else None


def set_status(job_id: int, status: str, error_code: str | None = None) -> None:
    with get_session() as s:
        job = s.get(Job, job_id)
        if job:
            job.status, job.error_code = status, error_code
            s.add(job)
            s.commit()


def retry_job(job_id: int) -> bool:
    with get_session() as s:
        job = s.get(Job, job_id)
        if not job or job.status not in ("failed", "cancelled", "dry_run"):
            return False
        job.status, job.run_at = "pending", utcnow()
        job.error = job.error_code = job.debug_dir = job.result_url = None
        s.add(job)
        s.commit()
    return True


def cancel_job(job_id: int) -> bool:
    """Pending and waiting-for-user jobs can be cancelled; a running upload finishes its step."""
    with get_session() as s:
        job = s.get(Job, job_id)
        if not job or job.status not in ("pending", "needs_action", "failed"):
            return False
        job.status, job.finished_at = "cancelled", utcnow()
        s.add(job)
        s.commit()
    return True


def interrupt_stale_jobs() -> int:
    """After a restart, jobs left 'running' may or may not be published: mark them failed for manual retry."""
    with get_session() as s:
        jobs = s.exec(select(Job).where(Job.status.in_(["running", "needs_action"]))).all()
        for job in jobs:
            job.status, job.error_code, job.finished_at = "failed", "interrupted", utcnow()
            s.add(job)
        s.commit()
        return len(jobs)


def daily_limit_wait(account_id: int) -> timedelta | None:
    """None if the account may publish now, else how long until a slot frees up (rolling 24 h)."""
    limit = settings_store.get_int("daily_limit")
    if limit <= 0:
        return None
    since = utcnow() - timedelta(hours=24)
    with get_session() as s:
        count, oldest = s.exec(
            select(func.count(Job.id), func.min(Job.finished_at))
            .where(Job.account_id == account_id, Job.status == "published", Job.finished_at >= since)
        ).one()
    if count < limit:
        return None
    return oldest + timedelta(hours=24) - utcnow()


def finish_job(job_id: int, result: PublishResult, debug_dir: str | None) -> None:
    with get_session() as s:
        job = s.get(Job, job_id)
        if job.status != "cancelled":  # user cancelled while we waited: keep it
            job.status = result.status
        job.result_url = result.url
        job.error_code, job.error = result.error_code, result.error
        job.debug_dir = debug_dir or job.debug_dir
        job.finished_at = utcnow()
        s.add(job)
        s.commit()


class Worker:
    def __init__(self, publishers: dict | None = None):
        self.publishers = publishers
        self.loop: asyncio.AbstractEventLoop | None = None
        self._event: asyncio.Event | None = None

    def wake(self) -> None:
        """Thread-safe: called by the scheduler and the web UI."""
        if self.loop and self._event:
            self.loop.call_soon_threadsafe(self._event.set)

    async def run_forever(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._event = asyncio.Event()
        while True:
            job_id = next_due_job_id()
            if job_id is None:
                try:
                    await asyncio.wait_for(self._event.wait(), timeout=POLL_SECONDS)
                except asyncio.TimeoutError:
                    pass
                self._event.clear()
                continue
            try:
                await self.run_job(job_id)
            except Exception:
                logger.exception("job %s crashed", job_id)
                finish_job(job_id, PublishResult("failed", error_code="exception"), None)
            if next_due_job_id() is not None:
                await browser.human_pause("job")

    async def run_job(self, job_id: int) -> None:
        publishers = self.publishers or default_publishers()
        with get_session() as s:
            job = s.get(Job, job_id)
            if not job or job.status != "pending":
                return
            account, post = s.get(Account, job.account_id), s.get(Post, job.post_id)
            wait = daily_limit_wait(account.id)
            if wait is not None:
                job.run_at = utcnow() + wait
                job.error_code = "daily_limit"
                s.add(job)
                s.commit()
                return
            job.status, job.started_at, job.attempts = "running", utcnow(), job.attempts + 1
            job.error = job.error_code = None
            s.add(job)
            s.commit()

        log = JobLog(job_id)
        log(f"start: {account.platform} / {account.name}, dry_run={post.dry_run}")
        video = Path(post.video_path)
        text = PlatformText.from_json(job.text_json)
        debug_dir = None
        try:
            publisher = publishers[account.platform](account, log)
            if not video.exists():
                raise FileNotFoundError(f"video not found: {video}")
            if publisher.uses_browser:
                result, debug_dir = await self._browser_publish(job_id, publisher, account, video, text,
                                                                post.dry_run, log)
            else:
                result = await publisher.publish(None, video, text, post.dry_run)
        except Exception as e:
            result = PublishResult("failed", error=f"{type(e).__name__}: {e}", error_code="exception")
        log(f"result: {result.status} {result.url or ''} {result.error or ''}".strip())
        if result.status == "failed" and debug_dir is None:
            debug_dir = await browser.save_debug(job_id, log.lines)
        finish_job(job_id, result, debug_dir)

    async def _browser_publish(self, job_id, publisher, account, video, text, dry_run, log):
        from playwright.async_api import async_playwright

        headless = settings_store.get_bool("headless")
        async with browser.BROWSER_LOCK, async_playwright() as pw:
            ctx = await browser.open_context(pw, account, headless, log)
            page = await browser.first_page(ctx)
            try:
                for _ in range(MAX_USER_ROUNDS):
                    try:
                        result = await publisher.publish(page, video, text, dry_run)
                    except NeedsUserAction as e:
                        log(f"user action needed: {e.code}")
                        if headless:
                            d = await browser.save_debug(job_id, log.lines, page)
                            return PublishResult("failed", error_code=f"{e.code}_headless"), d
                        set_status(job_id, "needs_action", e.code)
                        check = e.check or (lambda: publisher.is_logged_in(page))
                        ok = await browser.wait_until(
                            check, settings_store.get_float("user_wait_minutes"),
                            should_abort=lambda: page.is_closed() or job_status(job_id) == "cancelled",
                        )
                        if not ok:
                            d = await browser.save_debug(job_id, log.lines, page)
                            return PublishResult("failed", error_code="user_timeout"), d
                        await browser.save_session(ctx, account)
                        set_status(job_id, "running")
                        log("resolved by user, starting again")
                        continue
                    if result.status == "failed":
                        return result, await browser.save_debug(job_id, log.lines, page)
                    await browser.save_session(ctx, account)  # keep the session file fresh
                    return result, None
                d = await browser.save_debug(job_id, log.lines, page)
                return PublishResult("failed", error_code="user_timeout"), d
            except Exception as e:
                log(f"error: {type(e).__name__}: {e}")
                d = await browser.save_debug(job_id, log.lines, page)
                code = "network" if "net::ERR_" in str(e) else "exception"
                return PublishResult("failed", error=f"{type(e).__name__}: {str(e).splitlines()[0]}",
                                     error_code=code), d
            finally:
                try:
                    await ctx.close()
                except Exception:
                    pass


# ---- account login actions (run in the browser thread) ----

def _load_account(account_id: int) -> Account:
    with get_session() as s:
        return s.get(Account, account_id)


async def login_account(account_id: int, publishers: dict | None = None) -> str:
    """Open a visible browser on the platform login page and wait until the user logs in.
    On success the session file with the platform keys is saved."""
    from playwright.async_api import async_playwright

    publishers = publishers or default_publishers()
    login_state[account_id] = "queued"
    try:
        async with browser.BROWSER_LOCK, async_playwright() as pw:
            account = _load_account(account_id)
            publisher = publishers[account.platform](account, lambda m: None)
            login_state[account_id] = "waiting"
            ctx = await browser.open_context(pw, account, headless=False)
            try:
                page = await browser.first_page(ctx)
                await page.goto(publisher.login_url)
                ok = await browser.wait_until(lambda: publisher.is_logged_in(page),
                                              settings_store.get_float("user_wait_minutes"),
                                              should_abort=page.is_closed)
                if ok:
                    await asyncio.sleep(3)  # let redirects finish and cookies settle
                    await browser.save_session(ctx, account)
                login_state[account_id] = "ok" if ok else "timeout"
            finally:
                try:
                    await ctx.close()
                except Exception:
                    pass
    except Exception:
        login_state[account_id] = "error"
    return login_state[account_id]


async def check_login(account_id: int, publishers: dict | None = None) -> bool:
    from playwright.async_api import async_playwright

    publishers = publishers or default_publishers()
    account = _load_account(account_id)
    publisher = publishers[account.platform](account, lambda m: None)
    if not publisher.uses_browser:
        ok = await publisher.is_logged_in(None)
        browser.mark_logged_in(account_id, ok)
        return ok
    async with browser.BROWSER_LOCK, async_playwright() as pw:
        ctx = await browser.open_context(pw, account, settings_store.get_bool("headless"))
        try:
            page = await browser.first_page(ctx)
            await page.goto(publisher.check_url)
            await page.wait_for_timeout(2000)
            ok = await publisher.is_logged_in(page)
            if ok:
                await browser.save_session(ctx, account)
            else:
                browser.mark_logged_in(account_id, False)
            return ok
        finally:
            await ctx.close()
