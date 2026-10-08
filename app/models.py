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
    signature: str = ""  # appended to every description of this account
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
    group_id: int | None = None
    created_at: datetime = Field(default_factory=utcnow)


class AccountGroup(SQLModel, table=True):
    """A named set of accounts with optional posting slots and an inbox folder."""
    id: int | None = Field(default=None, primary_key=True)
    name: str
    slug: str  # inbox subfolder name
    account_ids: str = "[]"  # JSON list
    slots: str = ""  # local times, e.g. "10:00, 18:00"

    @property
    def ids(self) -> list[int]:
        return json.loads(self.account_ids or "[]")

    @property
    def slot_times(self) -> list[str]:
        return sorted(s.strip() for s in self.slots.replace(";", ",").split(",") if s.strip())


class Job(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    post_id: int = Field(foreign_key="post.id", index=True)
    account_id: int = Field(foreign_key="account.id", index=True)
    # pending | running | needs_action | published | dry_run | failed | cancelled
    status: str = Field(default="pending", index=True)
    dry_run: bool = True
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
