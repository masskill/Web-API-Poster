"""Unattended work: daily login checks, pre-publication checks, cleanup of old files."""
import logging
import shutil
import time
from datetime import datetime

from sqlmodel import select

from app import config, settings_store, worker
from app.db import get_session
from app.models import Account, Job, Post
from app.notify import message, notify
from app.platforms import platform_name

logger = logging.getLogger("poster.automation")

ACTIVE = ("pending", "running", "needs_action")


async def check_accounts(account_ids: list[int]) -> dict[int, bool]:
    """Check the login of the given accounts (hidden browser) and notify about lost sessions.
    Runs in the browser thread."""
    results = {}
    for account_id in account_ids:
        with get_session() as s:
            acc = s.get(Account, account_id)
        if acc is None:
            continue
        try:
            ok = await worker.check_login(account_id, headless=True)
        except Exception as e:
            logger.warning("login check of %s failed: %s", acc.name, e)
            continue  # network trouble is not a lost session
        results[account_id] = ok
        if not ok:
            await notify(message("notify.login_lost", who=f"{platform_name(acc.platform)} / {acc.name}"))
    return results


async def check_all_logins() -> dict[int, bool]:
    with get_session() as s:
        ids = [a.id for a in s.exec(select(Account)).all()]
    return await check_accounts(ids)


async def preflight(run_at: datetime) -> dict[int, bool]:
    """Some time before scheduled jobs: check that their accounts are still logged in."""
    with get_session() as s:
        ids = sorted({j.account_id for j in s.exec(select(Job).where(Job.status == "pending")).all()
                      if j.run_at == run_at})
    return await check_accounts(ids)


def cleanup(now: float | None = None) -> dict[str, int]:
    """Delete debug folders and videos older than keep_days that no active job needs.
    Videos uploaded but never used in a publication are deleted after one day."""
    keep_days = settings_store.get_int("keep_days")
    if keep_days <= 0:
        return {"debug": 0, "media": 0}
    now = now or time.time()
    cutoff = now - keep_days * 86400
    removed = {"debug": 0, "media": 0}
    if config.DEBUG_DIR.exists():
        for d in config.DEBUG_DIR.iterdir():
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                removed["debug"] += 1
    with get_session() as s:
        used = {p.video_path for p in s.exec(select(Post)).all()}
        active = {s.get(Post, j.post_id).video_path
                  for j in s.exec(select(Job).where(Job.status.in_(ACTIVE))).all() if s.get(Post, j.post_id)}
    if config.MEDIA_DIR.exists():
        for f in config.MEDIA_DIR.iterdir():
            if not f.is_file() or str(f) in active:
                continue
            age_limit = cutoff if str(f) in used else now - 86400
            if f.stat().st_mtime < age_limit:
                f.unlink(missing_ok=True)
                removed["media"] += 1
    if any(removed.values()):
        logger.info("cleanup: %s", removed)
    return removed
