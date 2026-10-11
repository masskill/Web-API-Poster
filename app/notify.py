"""Optional Telegram notifications to the owner: published, failed, needs action, session lost."""
import httpx

from app import settings_store
from app.config import scrub
from app.i18n import translate


def message(key: str, **kwargs) -> str:
    return translate(settings_store.get("notify_lang") or "uk", key, **kwargs)


async def notify(text: str, transport: httpx.AsyncBaseTransport | None = None) -> str | None:
    """Send a message to the owner's chat. Returns None on success, else an error text. Never raises."""
    token = settings_store.get("notify_bot_token").strip()
    chat_id = settings_store.get("notify_chat_id").strip()
    if not token or not chat_id:
        return "not_configured"
    try:
        async with httpx.AsyncClient(timeout=20, transport=transport) as client:
            r = await client.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                  data={"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": "true"})
            data = r.json()
    except Exception as e:  # the token is part of the URL: never show it
        return scrub(f"{type(e).__name__}: {e}", [token])
    return None if data.get("ok") else str(data.get("description") or r.status_code)
