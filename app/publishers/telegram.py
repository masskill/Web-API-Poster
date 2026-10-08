"""Telegram channel via the official Bot API (no browser, so no selectors).

Account keys: bot_token and chat_id (@channel_username or -100... id).
The bot must be an admin of the channel with the right to post.
"""
from pathlib import Path

import httpx

from app.config import scrub
from app.platforms import load_platforms
from app.publishers.base import PublishResult
from app.texts import PlatformText

API_URL = "https://api.telegram.org/bot{token}/{method}"
UPLOAD_TIMEOUT = 600  # seconds


class TelegramError(Exception):
    pass


def message_url(chat: dict, message_id: int) -> str | None:
    if chat.get("username"):
        return f"https://t.me/{chat['username']}/{message_id}"
    chat_id = str(chat.get("id", ""))
    if chat_id.startswith("-100"):  # private channel
        return f"https://t.me/c/{chat_id[4:]}/{message_id}"
    return None


class TelegramPublisher:
    platform = "telegram"
    uses_browser = False
    login_url = ""
    check_url = ""

    def __init__(self, account, log, transport: httpx.AsyncBaseTransport | None = None):
        cfg = account.cfg
        self.token = cfg.get("bot_token", "")
        self.chat_id = cfg.get("chat_id", "")
        self.log = log
        self.transport = transport  # tests pass httpx.MockTransport

    def _client(self, timeout: float = 30) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, transport=self.transport)

    async def _call(self, client: httpx.AsyncClient, method: str, **kwargs) -> dict:
        try:
            r = await client.post(API_URL.format(token=self.token, method=method), **kwargs)
            data = r.json()
        except Exception as e:
            # httpx errors can contain the URL, and the URL contains the token
            raise TelegramError(scrub(f"{method}: {type(e).__name__}: {e}", [self.token])) from None
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description') or r.status_code}")
        return data["result"]

    async def is_logged_in(self, page=None) -> bool:
        try:
            async with self._client() as c:
                await self._call(c, "getMe")
                await self._call(c, "getChat", data={"chat_id": self.chat_id})
            return True
        except TelegramError as e:
            self.log(f"check failed: {e}")
            return False

    async def publish(self, page, video: Path, text: PlatformText, dry_run: bool) -> PublishResult:
        limit = load_platforms()["telegram"]["video"]["max_size_mb"]
        size_mb = video.stat().st_size / 1024 / 1024
        if size_mb > limit:
            return PublishResult("failed", error_code="file_too_large", error=f"{size_mb:.1f} MB > {limit} MB")
        try:
            async with self._client(UPLOAD_TIMEOUT) as c:
                me = await self._call(c, "getMe")
                self.log(f"bot: @{me.get('username')}")
                chat = await self._call(c, "getChat", data={"chat_id": self.chat_id})
                self.log(f"channel: {chat.get('title')}")
                if dry_run:
                    self.log("dry-run: sendVideo skipped")
                    return PublishResult("dry_run")
                self.log(f"sendVideo: {video.name}, {size_mb:.1f} MB")
                try:
                    with open(video, "rb") as f:
                        msg = await self._call(
                            c, "sendVideo",
                            data={"chat_id": self.chat_id, "caption": text.caption(), "supports_streaming": "true"},
                            files={"video": (video.name, f)},
                        )
                except TelegramError as e:
                    if "Timeout" in str(e):  # the file may have reached Telegram: never send it twice
                        return PublishResult("published", error_code="unconfirmed", error=str(e))
                    raise
        except TelegramError as e:
            return PublishResult("failed", error_code="telegram", error=str(e))
        return PublishResult("published", url=message_url(chat, msg["message_id"]))
