"""Tests for the audit fixes: migration, CSRF, password, AI cut-off, duplicates protection."""
import asyncio
import base64
import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app import config, db
from app.main import app
from app.models import Account, Job, Post
from app.platforms import load_platforms
from app.publishers.base import BrowserPublisher, PublishResult
from app.publishers.telegram import TelegramPublisher
from app.texts import PlatformText, adapt_texts
from app.worker import finish_job


def test_migration_adds_new_columns(tmp_path):
    path = tmp_path / "old.db"
    old = create_engine(f"sqlite:///{path}")
    with old.begin() as c:  # a DB created by the first version: no signature / dry_run / group_id
        c.execute(text("CREATE TABLE account (id INTEGER PRIMARY KEY, platform VARCHAR, name VARCHAR, "
                       "slug VARCHAR, config VARCHAR, logged_in_at DATETIME, created_at DATETIME)"))
        c.execute(text("INSERT INTO account (platform, name, slug, config, created_at) "
                       "VALUES ('youtube', 'A', 'a', '{}', '2026-01-01 00:00:00')"))
    db.init_db(f"sqlite:///{path}")
    with db.get_session() as s:
        acc = s.get(Account, 1)
    assert acc.signature == "" and acc.name == "A"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("app.db.init_db", lambda url=None: None)
    with TestClient(app) as c:
        yield c


def test_cross_site_post_is_blocked(client):
    r = client.post("/categories", data={"name": "X"}, headers={"origin": "https://evil.example"},
                    follow_redirects=False)
    assert r.status_code == 403
    r = client.post("/categories", data={"name": "X"}, headers={"origin": "http://testserver"},
                    follow_redirects=False)
    assert r.status_code == 303


def test_password_protects_everything(client, monkeypatch):
    monkeypatch.setattr(config, "APP_PASSWORD", "s3cret")
    assert client.get("/").status_code == 401
    good = base64.b64encode(b"me:s3cret").decode()
    bad = base64.b64encode(b"me:nope").decode()
    assert client.get("/", headers={"authorization": f"Basic {bad}"}).status_code == 401
    assert client.get("/", headers={"authorization": f"Basic {good}"}).status_code == 200


def test_ai_cut_off_reply_is_reported():
    def handler(request):
        return httpx.Response(200, json={"stop_reason": "max_tokens", "content": [{"type": "text", "text": "{"}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = {"ai_provider": "anthropic", "anthropic_api_key": "k", "anthropic_model": "m"}
    texts, err = asyncio.run(adapt_texts(PlatformText("T", "D"), "", ["youtube"], load_platforms(), settings, client))
    assert "cut off" in err and texts["youtube"].title == "T"


def test_finish_job_ignores_deleted_job():
    finish_job(12345, PublishResult("failed"), None)  # must not raise


def test_telegram_timeout_is_unconfirmed(tmp_path):
    def handler(request):
        if request.url.path.endswith("sendVideo"):
            raise httpx.ReadTimeout("timed out")
        result = {"getMe": {"username": "b"}, "getChat": {"id": 1, "title": "C"}}
        return httpx.Response(200, json={"ok": True, "result": result[request.url.path.rsplit("/", 1)[1]]})

    acc = Account(platform="telegram", name="N", slug="n", config=json.dumps({"bot_token": "1:x", "chat_id": "@c"}))
    video = tmp_path / "v.mp4"
    video.write_bytes(b"0")
    pub = TelegramPublisher(acc, lambda m: None, transport=httpx.MockTransport(handler))
    r = asyncio.run(pub.publish(None, video, PlatformText(description="x"), False))
    assert r.status == "published" and r.error_code == "unconfirmed"


class AfterSubmitTimeout(BrowserPublisher):
    """Presses the final button, then waits for an element that never appears."""
    platform = "local"

    async def is_logged_in(self, page):
        return True

    async def _publish(self, page, video, text, dry_run):
        await page.set_content("<button id=b>Post</button>")
        await self.submit(page, "#b")
        await page.locator("#never").wait_for(timeout=500)
        return PublishResult("published")


def test_failure_after_submit_is_not_retryable(tmp_path):
    from playwright.async_api import async_playwright

    from app import settings_store
    settings_store.set_value("action_pause_min", "0")
    settings_store.set_value("action_pause_max", "0")

    async def go():
        async with async_playwright() as pw:
            try:
                b = await pw.chromium.launch(executable_path=os.getenv("BROWSER_EXECUTABLE") or None)
            except Exception as e:
                pytest.skip(f"no browser: {e}")
            page = await b.new_page()
            pub = AfterSubmitTimeout(Account(platform="local", name="A", slug="a"), lambda m: None)
            result = await pub.publish(page, tmp_path, PlatformText(), False)
            await b.close()
            return result

    result = asyncio.run(go())
    assert result.status == "published" and result.error_code == "unconfirmed"
