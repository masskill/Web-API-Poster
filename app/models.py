"""Database tables. All datetimes are timezone-aware UTC."""
import json
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Account(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    platform: str = Field(index=True)
    name: str
    slug: str
    # Platform-specific keys (Telegram: bot_token, chat_id). Never shown in full.
    config: str = "{}"
    logged_in_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def cfg(self) -> dict:
        return json.loads(self.config or "{}")


class Category(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str


class Post(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    video_path: str
    title: str = ""
    description: str = ""
    hashtags: str = ""
    category: str = ""
    scheduled_at: datetime | None = None
    dry_run: bool = True
    created_at: datetime = Field(default_factory=utcnow)


class Job(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    post_id: int = Field(foreign_key="post.id", index=True)
    account_id: int = Field(foreign_key="account.id", index=True)
    # pending | running | needs_action | published | dry_run | failed | cancelled
    status: str = Field(default="pending", index=True)
    run_at: datetime = Field(default_factory=utcnow)
    text_json: str = "{}"
    result_url: str | None = None
    error_code: str | None = None
    error: str | None = None
    debug_dir: str | None = None
    log: str = ""
    attempts: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None


class Setting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str = ""
