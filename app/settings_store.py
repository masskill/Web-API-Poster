"""User settings stored in the DB, with defaults from .env."""
import os

from app.db import get_session
from app.models import Setting

DEFAULTS = {
    "ai_provider": os.getenv("AI_PROVIDER", "none"),  # none | openai | anthropic
    "openai_api_key": os.getenv("OPENAI_API_KEY", ""),
    "openai_model": os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
    "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY", ""),
    "anthropic_model": os.getenv("ANTHROPIC_MODEL", "claude-haiku-5-5"),
    "browser_channel": os.getenv("BROWSER_CHANNEL", "msedge"),
    "headless": os.getenv("HEADLESS", "0"),
    "dry_run": os.getenv("DRY_RUN", "1"),
    "timezone": os.getenv("TIMEZONE", "Europe/Kyiv"),
    "daily_limit": "5",  # publications per account per 24 hours
    "user_wait_minutes": "10",  # how long to wait for captcha / login in visible mode
    "action_pause_min": "1.5",  # seconds between browser actions
    "action_pause_max": "4",
    "job_pause_min": "20",  # seconds between jobs
    "job_pause_max": "60",
    # notifications to the owner via a Telegram bot
    "notify_bot_token": os.getenv("NOTIFY_BOT_TOKEN", ""),
    "notify_chat_id": os.getenv("NOTIFY_CHAT_ID", ""),
    "notify_success": "1",  # also notify about successful publications
    "notify_lang": "uk",
    # automation
    "auto_retry": "2",  # automatic retries after network / upload / page errors
    "auto_retry_minutes": "10, 30",  # delay before each retry
    "login_check_time": "09:00",  # daily login check of all accounts (local time, empty = off)
    "keep_days": "30",  # debug files and finished videos are deleted after N days (0 = never)
    "inbox_dir": "",  # folder for the content inbox (empty = data/inbox)
}

SECRET_KEYS = {"openai_api_key", "anthropic_api_key", "notify_bot_token"}
BOOL_KEYS = {"headless", "dry_run", "notify_success"}


def get(key: str) -> str:
    with get_session() as s:
        row = s.get(Setting, key)
    return row.value if row is not None else DEFAULTS.get(key, "")


def get_bool(key: str) -> bool:
    return get(key).strip().lower() in ("1", "true", "yes", "on")


def get_int(key: str) -> int:
    try:
        return int(float(get(key)))
    except ValueError:
        return int(float(DEFAULTS[key]))


def get_float(key: str) -> float:
    try:
        return float(get(key))
    except ValueError:
        return float(DEFAULTS[key])


def set_value(key: str, value: str) -> None:
    with get_session() as s:
        row = s.get(Setting, key) or Setting(key=key)
        row.value = value
        s.add(row)
        s.commit()


def all_settings() -> dict[str, str]:
    return {k: get(k) for k in DEFAULTS}
