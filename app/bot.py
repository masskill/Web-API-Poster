"""Control from the phone through the notification bot (Settings -> Telegram notifications).

Send the bot a video (caption: first line = title, #hashtags, the rest = description), press the button
of a group, and the poster creates a publication in the group's next free slot. /status shows the queue.
Only the owner's chat (notify_chat_id) is obeyed; anyone else gets just their chat_id for the setup.
"""
import json
import logging
from datetime import timedelta
from pathlib import Path

import httpx
from sqlmodel import func, select

from app import config, posting, settings_store
from app.automation import parse_sidecar
from app.config import scrub
from app.db import get_session
from app.models import AccountGroup, Generation, Job, Mix, Post, utcnow
from app.notify import message
from app.platforms import load_platforms
from app.texts import PlatformText, adapt_texts

logger = logging.getLogger("poster.bot")

API_URL = "https://api.telegram.org/bot{token}/{method}"
FILE_URL = "https://api.telegram.org/file/bot{token}/{path}"
MAX_DOWNLOAD_MB = 20  # the Bot API lets a bot download files up to 20 MB


class BotError(Exception):
    pass


class Bot:
    def __init__(self, token: str, client: httpx.AsyncClient):
        self.token, self.client = token, client

    async def call(self, method: str, **data) -> dict | list:
        try:
            r = await self.client.post(API_URL.format(token=self.token, method=method), data=data)
            result = r.json()
        except Exception as e:  # the URL contains the token: never show it
            raise BotError(scrub(f"{method}: {type(e).__name__}: {e}", [self.token])) from None
        if not result.get("ok"):
            raise BotError(f"{method}: {result.get('description') or r.status_code}")
        return result["result"]

    async def send(self, chat_id, text: str, **extra) -> None:
        await self.call("sendMessage", chat_id=chat_id, text=text[:4000], **extra)

    async def download(self, file_id: str, dest: Path) -> None:
        info = await self.call("getFile", file_id=file_id)
        try:
            async with self.client.stream("GET", FILE_URL.format(token=self.token, path=info["file_path"])) as r:
                r.raise_for_status()
                with open(dest, "wb") as f:
                    async for chunk in r.aiter_bytes():
                        f.write(chunk)
        except httpx.HTTPError as e:
            dest.unlink(missing_ok=True)
            raise BotError(scrub(f"download: {type(e).__name__}: {e}", [self.token])) from None


def video_of(msg: dict) -> dict | None:
    """The video of a message: sent as a video, or as a file of a video type."""
    doc = msg.get("document") or {}
    return msg.get("video") or (doc if str(doc.get("mime_type", "")).startswith("video/") else None)


def status_text() -> str:
    """Short queue summary for /status."""
    since = utcnow() - timedelta(hours=24)
    with get_session() as s:
        def count(*where) -> int:
            return s.exec(select(func.count(Job.id)).where(*where)).one()
        pending = count(Job.status == "pending")
        running = count(Job.status.in_(["running", "needs_action"]))
        published = count(Job.status == "published", Job.finished_at >= since)
        failed = count(Job.status == "failed", Job.finished_at >= since)
        nxt = s.exec(select(Job).where(Job.status == "pending").order_by(Job.run_at)).first()
        post = s.get(Post, nxt.post_id) if nxt else None
        making = (s.exec(select(func.count(Generation.id)).where(Generation.status.in_(["queued", "running"]))).one()
                  + s.exec(select(func.count(Mix.id)).where(Mix.status.in_(["queued", "running"]))).one())
    text = message("bot.status", pending=pending, running=running, published=published, failed=failed, making=making)
    if nxt:
        when = nxt.run_at.astimezone(posting.local_tz()).strftime("%d.%m %H:%M")
        text += "\n" + message("bot.next", time=when, title=(post.title if post and post.title else "—"))
    return text


async def handle_message(bot: Bot, msg: dict, owner: str) -> None:
    chat_id = msg.get("chat", {}).get("id")
    text = (msg.get("text") or "").strip()
    if str(chat_id) != owner:  # a stranger: only help with the setup, never act
        if text.startswith("/start"):
            await bot.send(chat_id, message("bot.your_id", id=chat_id))
        return
    video = video_of(msg)
    if video:
        if (video.get("file_size") or 0) > MAX_DOWNLOAD_MB * 1024 * 1024:
            await bot.send(chat_id, message("bot.too_big", mb=MAX_DOWNLOAD_MB), reply_to_message_id=msg["message_id"])
            return
        with get_session() as s:
            groups = s.exec(select(AccountGroup).order_by(AccountGroup.name)).all()
        if not groups:
            await bot.send(chat_id, message("bot.no_groups"))
            return
        buttons = [[{"text": g.name, "callback_data": f"g:{g.id}"}] for g in groups]
        await bot.send(chat_id, message("bot.pick_group"), reply_to_message_id=msg["message_id"],
                       reply_markup=json.dumps({"inline_keyboard": buttons}))
    elif text.startswith("/status"):
        await bot.send(chat_id, status_text())
    else:
        await bot.send(chat_id, message("bot.help", id=chat_id))


async def handle_callback(bot: Bot, cq: dict, owner: str, on_created=None) -> Post | None:
    """A group button under a video was pressed: download the video and create the publication."""
    msg = cq.get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    if str(chat_id) != owner or not str(cq.get("data", "")).startswith("g:"):
        return None
    await bot.call("answerCallbackQuery", callback_query_id=cq["id"])
    original = msg.get("reply_to_message") or {}
    video = video_of(original)
    group_id = str(cq["data"])[2:]
    with get_session() as s:
        group = s.get(AccountGroup, int(group_id)) if group_id.isdigit() else None
    accounts = posting.group_accounts(group) if group else []
    if not video or not accounts:
        await bot.call("editMessageText", chat_id=chat_id, message_id=msg["message_id"], text=message("bot.failed"))
        return None
    # Remove the buttons first, so a second press cannot create a second publication.
    await bot.call("editMessageText", chat_id=chat_id, message_id=msg["message_id"], text=message("bot.downloading"))
    filename = video.get("file_name") or f"telegram_{original.get('message_id', 0)}.mp4"
    dest = config.MEDIA_DIR / posting.media_name(filename)
    config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    await bot.download(video["file_id"], dest)
    caption = original.get("caption") or ""
    template = parse_sidecar(caption, Path(filename).stem) if caption.strip() else PlatformText(title=Path(filename).stem)
    keys = list(dict.fromkeys(a.platform for a in accounts))
    texts, _ = await adapt_texts(template, group.name, keys, load_platforms(), settings_store.all_settings())
    run_at = posting.next_free_slot(group)
    post = posting.create_post(dest, template, accounts, run_at, settings_store.get_bool("dry_run"),
                               category=group.name, texts=texts, group_id=group.id)
    when = (run_at or post.created_at).astimezone(posting.local_tz()).strftime("%d.%m %H:%M")
    await bot.call("editMessageText", chat_id=chat_id, message_id=msg["message_id"],
                   text=message("bot.created", title=post.title or "—", group=group.name, time=when,
                                n=len(accounts), dry_run=" (dry-run)" if post.dry_run else ""))
    if on_created:
        on_created(run_at or post.created_at)
    return post


async def poll(transport: httpx.AsyncBaseTransport | None = None, on_created=None) -> int:
    """Called every few seconds by the scheduler; returns how many updates were handled."""
    token = settings_store.get("notify_bot_token").strip()
    owner = settings_store.get("notify_chat_id").strip()
    if not token or not settings_store.get_bool("bot_control"):
        return 0
    async with httpx.AsyncClient(timeout=60, transport=transport) as client:
        bot = Bot(token, client)
        offset = int(settings_store.get("bot_offset") or 0)
        try:
            updates = await bot.call("getUpdates", offset=offset, timeout=0,
                                     allowed_updates=json.dumps(["message", "callback_query"]))
        except BotError as e:
            logger.info("bot: %s", e)
            return 0
        for update in updates:
            settings_store.set_value("bot_offset", str(update["update_id"] + 1))  # never handle an update twice
            try:
                if "message" in update:
                    await handle_message(bot, update["message"], owner)
                elif "callback_query" in update:
                    await handle_callback(bot, update["callback_query"], owner, on_created)
            except Exception as e:
                logger.warning("bot: update %s failed: %s", update.get("update_id"), scrub(str(e), [token]))
                chat = (update.get("message") or update.get("callback_query", {}).get("message") or {}).get("chat", {})
                if str(chat.get("id")) == owner:
                    try:
                        await bot.send(owner, message("bot.error", error=scrub(str(e), [token])[:300]))
                    except BotError:
                        pass
        return len(updates)
