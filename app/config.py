"""Paths and environment configuration."""
import os
import re
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DATA_DIR = Path(os.getenv("DATA_DIR") or BASE_DIR / "data")
MEDIA_DIR = DATA_DIR / "media"
PROFILES_DIR = DATA_DIR / "profiles"
SESSIONS_DIR = DATA_DIR / "sessions"
DEBUG_DIR = DATA_DIR / "debug"
DB_PATH = DATA_DIR / "app.db"
PLATFORMS_FILE = BASE_DIR / "platforms.yaml"
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def ensure_dirs() -> None:
    for d in (DATA_DIR, MEDIA_DIR, PROFILES_DIR, SESSIONS_DIR, DEBUG_DIR):
        d.mkdir(parents=True, exist_ok=True)


def mask_secret(value: str | None) -> str:
    """Show only the edges of a secret: 'abcd…wxyz'."""
    if not value:
        return ""
    if len(value) <= 10:
        return "•" * len(value)
    return f"{value[:4]}…{value[-4:]}"


def scrub(text: str, secrets: list[str | None]) -> str:
    """Replace every known secret in text with its mask."""
    for s in secrets:
        if s:
            text = text.replace(s, mask_secret(s))
    return text


def slugify(name: str) -> str:
    slug = re.sub(r"[^\w-]+", "-", name.strip().lower(), flags=re.UNICODE).strip("-_")
    return slug or "account"
