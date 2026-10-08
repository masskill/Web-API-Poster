"""Unattended work: content inbox, daily login checks, pre-publication checks, cleanup of old files."""
import asyncio
import logging
import shutil
import time
from datetime import datetime
from pathlib import Path

from sqlmodel import select

from app import config, posting, settings_store, worker
from app.db import get_session
from app.models import Account, AccountGroup, Job, Post
from app.notify import message, notify
from app.platforms import load_platforms, platform_name
from app.texts import PlatformText, adapt_texts, parse_hashtags

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


# ---- content inbox: data/inbox/<group slug>/video.mp4 (+ video.txt) -> publication ----

VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
_seen_sizes: dict[str, int] = {}  # a file is taken only when its size did not change since the last scan


def inbox_dir() -> Path:
    return Path(settings_store.get("inbox_dir").strip() or config.DATA_DIR / "inbox")


def read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:  # Notepad "ANSI" on a Ukrainian / Russian Windows
        return path.read_text(encoding="cp1251")


def parse_sidecar(raw: str, fallback_title: str = "") -> PlatformText:
    """First line: title. Lines made only of #hashtags: hashtags. Everything else: description."""
    lines = [line.rstrip() for line in raw.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    title = lines.pop(0).strip() if lines else fallback_title
    tags, body = [], []
    for line in lines:
        words = line.split()
        if words and all(w.startswith("#") for w in words):
            tags += words
        else:
            body.append(line)
    return PlatformText(title=title, description="\n".join(body).strip(), hashtags=parse_hashtags(tags))


def import_video(video: Path, group: AccountGroup) -> Post | None:
    accounts = posting.group_accounts(group)
    if not accounts:
        logger.warning("inbox: group %s has no accounts, %s left in place", group.name, video.name)
        return None
    sidecar = video.with_suffix(".txt")
    template = (parse_sidecar(read_text_file(sidecar), video.stem) if sidecar.exists()
                else PlatformText(title=video.stem))
    dest = config.MEDIA_DIR / posting.media_name(video.name)
    config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    shutil.move(str(video), dest)
    if sidecar.exists():
        done = video.parent / "_done"
        done.mkdir(exist_ok=True)
        shutil.move(str(sidecar), done / f"{dest.stem}.txt")
    keys = list(dict.fromkeys(a.platform for a in accounts))
    texts, _ = asyncio.run(adapt_texts(template, group.name, keys, load_platforms(), settings_store.all_settings()))
    run_at = posting.next_free_slot(group)
    return posting.create_post(dest, template, accounts, run_at, settings_store.get_bool("dry_run"),
                               category=group.name, texts=texts, group_id=group.id)


def scan_inbox(on_created=None) -> list[Post]:
    """Called every minute by the scheduler. on_created(run_at) wakes the worker / schedules the post."""
    base = inbox_dir()
    with get_session() as s:
        groups = s.exec(select(AccountGroup)).all()
    created = []
    for group in groups:
        folder = base / group.slug
        folder.mkdir(parents=True, exist_ok=True)
        for video in sorted(folder.iterdir()):
            if not video.is_file() or video.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            size = video.stat().st_size
            if _seen_sizes.get(str(video)) != size:  # new or still being copied: take it next time
                _seen_sizes[str(video)] = size
                continue
            _seen_sizes.pop(str(video), None)
            try:
                post = import_video(video, group)
            except Exception:
                logger.exception("inbox: cannot import %s", video)
                continue
            if post is None:
                continue
            created.append(post)
            run_at = post.scheduled_at or post.created_at
            if on_created:
                on_created(run_at)
            asyncio.run(notify(message("notify.inbox", title=post.title or video.stem, n=len(group.ids),
                                       time=run_at.astimezone(posting.local_tz()).strftime("%d.%m %H:%M"))))
    return created
