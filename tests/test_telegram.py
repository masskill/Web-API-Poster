import asyncio
import json

import httpx

from app.models import Account
from app.platforms import load_platforms
from app.publishers.telegram import TelegramPublisher, message_url
from app.texts import PlatformText

TOKEN = "123456:ABCdefSecretToken"


def make(handler, logs=None):
    acc = Account(platform="telegram", name="News", slug="news",
                  config=json.dumps({"bot_token": TOKEN, "chat_id": "@news"}))
    return TelegramPublisher(acc, (logs if logs is not None else []).append, transport=httpx.MockTransport(handler))


def api(calls):
    def handler(request: httpx.Request):
        method = request.url.path.rsplit("/", 1)[1]
        calls.append(method)
        result = {
            "getMe": {"username": "poster_bot"},
            "getChat": {"id": -1001234567890, "title": "News", "username": "news"},
            "sendVideo": {"message_id": 42},
        }[method]
        return httpx.Response(200, json={"ok": True, "result": result})
    return handler


def video_file(tmp_path):
    f = tmp_path / "v.mp4"
    f.write_bytes(b"0" * 1024)
    return f


def test_dry_run_does_not_send(tmp_path):
    calls = []
    r = asyncio.run(make(api(calls)).publish(None, video_file(tmp_path), PlatformText(description="x"), True))
    assert r.status == "dry_run" and calls == ["getMe", "getChat"]


def test_publish_returns_post_url(tmp_path):
    calls = []
    text = PlatformText(description="Новий рецепт", hashtags=["борщ"])
    r = asyncio.run(make(api(calls)).publish(None, video_file(tmp_path), text, False))
    assert r.status == "published" and r.url == "https://t.me/news/42"
    assert calls == ["getMe", "getChat", "sendVideo"]


def test_private_channel_url():
    assert message_url({"id": -1001234567890}, 7) == "https://t.me/c/1234567890/7"


def test_api_error_is_reported():
    def handler(request):
        return httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"})
    logs = []
    assert asyncio.run(make(handler, logs).is_logged_in()) is False
    assert "chat not found" in logs[0]


def test_network_error_hides_token(tmp_path):
    def handler(request):
        raise httpx.ConnectError(f"cannot connect to {request.url}")
    r = asyncio.run(make(handler).publish(None, video_file(tmp_path), PlatformText(), False))
    assert r.status == "failed" and r.error_code == "telegram"
    assert TOKEN not in r.error and "1234…oken" in r.error


def test_too_large_file(tmp_path, monkeypatch):
    monkeypatch.setitem(load_platforms()["telegram"]["video"], "max_size_mb", 0.0001)
    r = asyncio.run(make(api([])).publish(None, video_file(tmp_path), PlatformText(), False))
    assert r.status == "failed" and r.error_code == "file_too_large"
