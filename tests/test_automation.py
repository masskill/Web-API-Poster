"""Unattended work: notifications, cleanup, pre-publication checks, automation schedule."""
import asyncio
import os
import re
import time
from datetime import timedelta
from pathlib import Path

import httpx

from app import automation, config, settings_store
from app.db import get_session
from app.i18n import TEXTS
from app.models import Account, Job, Post, utcnow
from app.notify import message, notify
from app.scheduler import Scheduler


def test_notify_sends_message_and_hides_token():
    settings_store.set_value("notify_bot_token", "999:SECRETTOKEN")
    settings_store.set_value("notify_chat_id", "42")
    seen = {}

    def ok(request):
        seen["path"], seen["body"] = request.url.path, request.content.decode()
        return httpx.Response(200, json={"ok": True, "result": {}})

    assert asyncio.run(notify("hello", transport=httpx.MockTransport(ok))) is None
    assert seen["path"].endswith("/sendMessage") and "chat_id=42" in seen["body"]

    def down(request):
        raise httpx.ConnectError(f"no route to {request.url}")

    error = asyncio.run(notify("hello", transport=httpx.MockTransport(down)))
    assert error and "SECRETTOKEN" not in error


def test_notify_not_configured():
    assert asyncio.run(notify("x")) == "not_configured"


def test_all_notification_messages_are_translated():
    keys = set()
    for f in Path(config.BASE_DIR, "app").rglob("*.py"):
        keys |= set(re.findall(r'message\(\s*"(notify\.\w+)"', f.read_text(encoding="utf-8")))
    assert keys and keys <= set(TEXTS)
    assert "{who}" in message("notify.published", who="{who}", url="")


def old(path: Path, days: float) -> None:
    t = time.time() - days * 86400
    os.utime(path, (t, t))


def test_cleanup_keeps_active_and_recent_files():
    settings_store.set_value("keep_days", "30")
    config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    config.DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    active, finished, orphan, fresh = (config.MEDIA_DIR / n for n in ("a.mp4", "f.mp4", "o.mp4", "n.mp4"))
    for f in (active, finished, orphan, fresh):
        f.write_bytes(b"0")
    for f in (active, finished):
        old(f, 40)
    old(orphan, 2)
    with get_session() as s:
        acc = Account(platform="youtube", name="A", slug="a")
        s.add(acc)
        p1, p2 = Post(video_path=str(active)), Post(video_path=str(finished))
        s.add(p1)
        s.add(p2)
        s.commit()
        s.add(Job(post_id=p1.id, account_id=acc.id, status="pending"))
        s.add(Job(post_id=p2.id, account_id=acc.id, status="published"))
        s.commit()
    debug_old = config.DEBUG_DIR / "777"
    debug_old.mkdir()
    old(debug_old, 40)
    removed = automation.cleanup()
    assert active.exists() and fresh.exists()
    assert not finished.exists() and not orphan.exists() and not debug_old.exists()
    assert removed == {"debug": 1, "media": 2}


def test_preflight_checks_accounts_of_that_time(monkeypatch):
    checked = []

    async def fake_check(account_id, publishers=None, headless=None):
        checked.append(account_id)
        return account_id != 2

    sent = []

    async def fake_notify(text, transport=None):
        sent.append(text)

    monkeypatch.setattr("app.worker.check_login", fake_check)
    monkeypatch.setattr("app.automation.notify", fake_notify)
    run_at = utcnow() + timedelta(hours=1)
    with get_session() as s:
        for i in (1, 2, 3):
            s.add(Account(platform="youtube", name=f"A{i}", slug=f"a{i}"))
        post = Post(video_path="v.mp4")
        s.add(post)
        s.commit()
        s.add(Job(post_id=post.id, account_id=1, run_at=run_at))
        s.add(Job(post_id=post.id, account_id=2, run_at=run_at))
        s.add(Job(post_id=post.id, account_id=3, run_at=run_at + timedelta(hours=5)))
        s.commit()
    with get_session() as s:
        stored = s.get(Job, 1).run_at
    results = asyncio.run(automation.preflight(stored))
    assert checked == [1, 2] and results == {1: True, 2: False}
    assert len(sent) == 1 and "A2" in sent[0]


def test_scheduler_registers_preflight_and_automation():
    settings_store.set_value("login_check_time", "09:15")
    sched = Scheduler(lambda: None, preflight=lambda run_at: None)
    try:
        sched.aps.start()
        sched.schedule(utcnow() + timedelta(hours=2))
        sched.start_automation(login_check=lambda: None, cleanup=lambda: None)
        ids = {j.id for j in sched.aps.get_jobs()}
        assert {"login-check", "cleanup"} <= ids
        assert any(i.startswith("preflight-") for i in ids) and any(i.startswith("wake-") for i in ids)
        settings_store.set_value("login_check_time", "")
        sched.start_automation(login_check=lambda: None, cleanup=lambda: None)
        assert sched.aps.get_job("login-check") is None
    finally:
        sched.shutdown()
