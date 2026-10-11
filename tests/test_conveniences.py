"""Version 1.7: cover image, all-platform test, backup, update check, calendar, proxy, Telegram shrink, bot control."""
import asyncio
import io
import json
import shutil
import sqlite3
import subprocess
import time
import urllib.parse
import zipfile
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app import automation, bot, browser, config, settings_store, worker
from app.db import get_session
from app.main import app
from app.models import Account, AccountGroup, Job, Post
from app.posting import create_post, local_tz
from app.publishers.base import PublishResult
from app.publishers.telegram import TelegramPublisher
from app.texts import PlatformText
from app.video import probe, shrink_plan

HAS_FFMPEG = bool(shutil.which("ffmpeg"))
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg is not installed")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("app.db.init_db", lambda url=None: None)  # keep the in-memory test DB
    with TestClient(app) as c:
        yield c


def add_account(platform="youtube", name="Music", **cfg) -> Account:
    with get_session() as s:
        acc = Account(platform=platform, name=name, slug=name.lower(), config=json.dumps(cfg))
        s.add(acc)
        s.commit()
        return acc


LATER = {"when": "at", "scheduled_at": "2030-01-01T10:00"}  # the worker must not run these posts


def media_video() -> str:
    config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy(config.ASSETS_DIR / "test_video.mp4", config.MEDIA_DIR / "cover_test.mp4")
    return "cover_test.mp4"


# ---- cover image ----

@needs_ffmpeg
def test_cover_from_video_frame_is_kept_and_deleted_with_post(client):
    acc = add_account()
    r = client.post("/posts", data={"video_name": media_video(), "account_ids": acc.id, "title": "Mix",
                                    "thumb_second": "1", **LATER}, follow_redirects=False)
    assert r.headers["location"] == "/posts?created=1"
    with get_session() as s:
        post = s.exec(select(Post)).one()
    cover = config.MEDIA_DIR / post.thumbnail_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    assert cover.exists() and cover.suffix == ".jpg"
    automation.cleanup(now=time.time() + 3 * 86400)  # the post still waits: its cover must stay
    assert cover.exists()
    worker.delete_post(post.id)
    assert not cover.exists()


def test_cover_errors(client):
    acc = add_account()
    name = media_video()
    for extra, files in (({"thumb_second": "abc"}, None), ({}, {"thumbnail": ("c.gif", b"GIF", "image/gif")})):
        r = client.post("/posts", data={"video_name": name, "account_ids": acc.id, "when": "now", **extra},
                        files=files, follow_redirects=False)
        assert r.headers["location"].endswith("post.err_thumbnail")


def test_uploaded_cover_reaches_the_publisher(client, tmp_path):
    acc = add_account()
    r = client.post("/posts", data={"video_name": media_video(), "account_ids": acc.id, **LATER},
                    files={"thumbnail": ("cover.png", b"PNG", "image/png")}, follow_redirects=False)
    assert r.headers["location"] == "/posts?created=1"
    with get_session() as s:
        post = s.exec(select(Post)).one()
    cover = tmp_path / "c.png"
    assert open(post.thumbnail_path, "rb").read() == b"PNG" and post.thumbnail_path.endswith("cover.png")
    cover.write_bytes(b"PNG")
    post = create_post(config.MEDIA_DIR / "cover_test.mp4", PlatformText("t"), [acc], None, True, thumbnail=str(cover))
    seen = {}

    class Fake:
        uses_browser = False

        def __init__(self, account, log):
            pass

        async def publish(self, page, video, text, dry_run):
            seen["thumbnail"] = self.thumbnail
            return PublishResult("dry_run")

    with get_session() as s:
        job_id = s.exec(select(Job.id).where(Job.post_id == post.id)).one()
    asyncio.run(worker.Worker({"youtube": Fake}).run_job(job_id))
    assert seen["thumbnail"] == cover


def test_test_all_accounts_creates_one_dry_run(client, monkeypatch):
    monkeypatch.setattr(worker.Worker, "wake", lambda self: None)  # do not start a real browser
    add_account("youtube", "A")
    add_account("tiktok", "B")
    r = client.post("/accounts/test-all")
    assert r.headers["HX-Redirect"] == "/posts"
    with get_session() as s:
        jobs = s.exec(select(Job)).all()
    assert len(jobs) == 2 and all(j.dry_run for j in jobs)


# ---- backup ----

def test_backup_contains_database_and_sessions(client):
    add_account(name="Backup")
    session = config.SESSIONS_DIR / "youtube" / "backup.json"
    session.parent.mkdir(parents=True, exist_ok=True)
    session.write_text('{"cookies": []}', encoding="utf-8")
    r = client.get("/settings/backup")
    assert r.headers["content-type"] == "application/zip"
    assert "web-api-poster-backup-" in r.headers["content-disposition"]
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert "sessions/youtube/backup.json" in z.namelist()
    db_file = config.DATA_DIR / "restored.db"
    db_file.write_bytes(z.read("app.db"))
    names = [row[0] for row in sqlite3.connect(db_file).execute("select name from account")]
    assert "Backup" in names


# ---- update check ----

def test_update_check_and_badge(client):
    changelog = "# Оновлення\n\n## 9.1.0 — 01.01.2027\n\n- new\n\n## 1.0.0\n"
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, text=changelog)

    assert asyncio.run(automation.check_update(httpx.MockTransport(handler))) == "9.1.0"
    assert seen[0] == "https://raw.githubusercontent.com/masskill/Web-API-Poster/main/docs/CHANGELOG.md"
    assert "Оновлення 9.1.0" in client.get("/").text
    page = client.get("/updates").text
    assert "Вийшла нова версія 9.1.0" in page and "archive/refs/heads/main.zip" in page
    settings_store.set_value("latest_version", config.APP_VERSION)
    assert "Оновлення " + config.APP_VERSION not in client.get("/").text


def test_version_compare():
    assert automation.is_newer("1.10.0", "1.9.3")
    assert not automation.is_newer("1.7.0", "1.7.0")
    assert not automation.is_newer("", "1.7.0")
    assert automation.changelog_version("x\n## 2.0.1 — date") == "2.0.1"


# ---- calendar ----

def test_calendar_shows_month_with_statuses(client):
    acc = add_account()
    tz = local_tz()
    when = datetime(2026, 11, 14, 18, 0, tzinfo=tz).astimezone(timezone.utc)
    create_post(config.ASSETS_DIR / "test_video.mp4", PlatformText("Friday mix"), [acc], when, True)
    page = client.get("/schedule?month=2026-11").text
    assert "Листопад 2026" in page and "18:00 Friday mix" in page and "badge pending" in page
    assert "/schedule?month=2026-10" in page and "/schedule?month=2026-12" in page
    assert "Friday mix" not in client.get("/schedule?month=2026-09").text.split("Найближчі")[0]
    client.cookies.set("lang", "ru")
    assert "Ноябрь 2026" in client.get("/schedule?month=2026-11").text
    assert client.get("/schedule?month=bad").status_code == 200


# ---- proxy ----

def test_proxy_settings_and_label():
    assert browser.proxy_settings("") is None
    assert browser.proxy_settings("1.2.3.4:8080") == {"server": "http://1.2.3.4:8080"}
    p = browser.proxy_settings("http://us%40r:p%3Ass@proxy.example:3128")
    assert p == {"server": "http://proxy.example:3128", "username": "us@r", "password": "p:ss"}
    assert browser.proxy_label("socks5://u:secret@h:1080") == "socks5://h:1080"


def test_proxy_is_saved_masked_and_cleared(client):
    acc = add_account()
    client.post(f"/accounts/{acc.id}/edit", data={"name": "Music", "proxy": "http://u:secret@1.2.3.4:8080"})
    with get_session() as s:
        assert s.get(Account, acc.id).cfg["proxy"] == "http://u:secret@1.2.3.4:8080"
    assert "secret" not in client.get("/accounts").text and "http://1.2.3.4:8080" in client.get("/accounts").text
    client.post(f"/accounts/{acc.id}/edit", data={"name": "Music", "proxy": ""})  # empty: keep
    with get_session() as s:
        assert "proxy" in s.get(Account, acc.id).cfg
    r = client.post(f"/accounts/{acc.id}/edit", data={"name": "Music", "proxy": "1.2.3.4:port"}, follow_redirects=False)
    assert r.headers["location"].endswith("acc.err_proxy")
    client.post(f"/accounts/{acc.id}/edit", data={"name": "Music", "clear_proxy": "1"})
    with get_session() as s:
        assert "proxy" not in s.get(Account, acc.id).cfg


# ---- Telegram: shrinking a big video ----

def test_shrink_plan():
    assert shrink_plan(60, 45)[1] == 1080  # one minute: plenty of bitrate
    assert shrink_plan(600, 45)[1] == 480
    assert shrink_plan(7200, 45) is None  # two hours cannot fit into 45 MB


@needs_ffmpeg
def test_telegram_shrinks_big_video(tmp_path, monkeypatch):
    from app.platforms import load_platforms
    monkeypatch.setitem(load_platforms()["telegram"]["video"], "max_size_mb", 1)
    big = tmp_path / "big.mp4"  # 10 s of noise at a high bitrate: a few MB, above the 1 MB limit
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "nullsrc=s=640x360:d=10,geq=random(1)*255:128:128",
                    "-c:v", "libx264", "-b:v", "4M", "-pix_fmt", "yuv420p", str(big)], check=True)
    assert big.stat().st_size > 1024 * 1024
    sent = []

    def handler(request):
        method = request.url.path.rsplit("/", 1)[1]
        if method == "sendVideo":
            sent.append(len(request.content))
        result = {"getMe": {"username": "b"}, "getChat": {"id": -100, "title": "c", "username": "c"},
                  "sendVideo": {"message_id": 1}}[method]
        return httpx.Response(200, json={"ok": True, "result": result})

    acc = Account(platform="telegram", name="c", slug="c", config=json.dumps({"bot_token": "1:x", "chat_id": "@c"}))
    logs = []
    r = asyncio.run(TelegramPublisher(acc, logs.append, httpx.MockTransport(handler))
                    .publish(None, big, PlatformText(), False))
    assert r.status == "published", (r, logs)
    small = config.MEDIA_DIR / "big.tg.mp4"
    assert small.exists() and small.stat().st_size <= 1024 * 1024 and sent
    assert abs(probe(small).duration - 10) < 0.5


# ---- control through the Telegram bot ----

OWNER = "111"


def telegram(updates, calls):
    def handler(request: httpx.Request):
        if request.url.path.startswith("/file/"):
            calls.append(("file", request.url.path))
            return httpx.Response(200, content=b"video-bytes")
        method = request.url.path.rsplit("/", 1)[1]
        calls.append((method, dict(urllib.parse.parse_qsl(request.content.decode()))))
        result = {"getUpdates": updates, "getFile": {"file_path": "videos/file_1.mp4"}}.get(method, True)
        return httpx.Response(200, json={"ok": True, "result": result})
    return httpx.MockTransport(handler)


def setup_bot():
    settings_store.set_value("notify_bot_token", "123:SECRET")
    settings_store.set_value("notify_chat_id", OWNER)


def test_bot_obeys_only_the_owner():
    setup_bot()
    calls = []
    updates = [{"update_id": 7, "message": {"message_id": 1, "chat": {"id": 999}, "text": "/start"}},
               {"update_id": 8, "message": {"message_id": 2, "chat": {"id": 999}, "text": "/status"}}]
    assert asyncio.run(bot.poll(telegram(updates, calls))) == 2
    sent = [d for m, d in calls if m == "sendMessage"]
    assert len(sent) == 1 and "999" in sent[0]["text"]  # a stranger only gets his chat_id
    assert settings_store.get("bot_offset") == "9"
    assert json.loads(calls[0][1]["allowed_updates"]) == ["message", "callback_query"]


def test_bot_status_and_disabled():
    setup_bot()
    calls = []
    asyncio.run(bot.poll(telegram([{"update_id": 1, "message": {"message_id": 1, "chat": {"id": 111},
                                                                 "text": "/status"}}], calls)))
    assert "Черга" in calls[-1][1]["text"]
    settings_store.set_value("bot_control", "0")
    assert asyncio.run(bot.poll(telegram([], []))) == 0


def test_bot_video_to_publication():
    setup_bot()
    acc = add_account()
    with get_session() as s:
        group = AccountGroup(name="Музика", slug="music", account_ids=json.dumps([acc.id]))
        s.add(group)
        s.commit()
    video = {"file_id": "F1", "file_size": 1000, "file_name": "borshch.mp4"}
    calls = []
    msg = {"message_id": 4, "chat": {"id": 111}, "caption": "Борщ\nСмачний рецепт\n#борщ", "video": video}
    asyncio.run(bot.poll(telegram([{"update_id": 1, "message": msg}], calls)))
    keyboard = json.loads(calls[-1][1]["reply_markup"])["inline_keyboard"]
    assert keyboard == [[{"text": "Музика", "callback_data": f"g:{group.id}"}]]

    calls, created = [], []
    cq = {"id": "cb1", "data": f"g:{group.id}", "message": {"message_id": 5, "chat": {"id": 111}, "reply_to_message": msg}}
    asyncio.run(bot.poll(telegram([{"update_id": 2, "callback_query": cq}], calls), on_created=created.append))
    with get_session() as s:
        post = s.exec(select(Post)).one()
    assert post.title == "Борщ" and post.description == "Смачний рецепт" and post.dry_run
    assert open(post.video_path, "rb").read() == b"video-bytes" and created
    assert ("file", "/file/bot123:SECRET/videos/file_1.mp4") in calls
    assert "Борщ" in [d for m, d in calls if m == "editMessageText"][-1]["text"]


def test_bot_rejects_big_video_and_hides_token_in_errors():
    setup_bot()
    calls = []
    big = {"message_id": 1, "chat": {"id": 111}, "video": {"file_id": "F", "file_size": 50 * 1024 * 1024}}
    asyncio.run(bot.poll(telegram([{"update_id": 1, "message": big}], calls)))
    assert "20 МБ" in calls[-1][1]["text"]

    def broken(request):
        raise httpx.ConnectError(f"cannot reach {request.url}")
    assert asyncio.run(bot.poll(httpx.MockTransport(broken))) == 0  # logged, not raised
