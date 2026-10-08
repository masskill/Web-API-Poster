"""Creating publications (from the form, the inbox folder or a platform test) and posting slots."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlmodel import select

from app import settings_store
from app.db import get_session
from app.models import Account, AccountGroup, Job, Post, utcnow
from app.platforms import load_platforms
from app.texts import PlatformText, fit_to_rules


def local_tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings_store.get("timezone"))
    except Exception:
        return ZoneInfo("Europe/Kyiv")


def with_signature(text: PlatformText, account: Account, rules: dict) -> PlatformText:
    """Append the account signature and cut again to the platform limits."""
    if not account.signature.strip():
        return text
    signed = PlatformText(text.title, f"{text.description.rstrip()}\n\n{account.signature.strip()}".strip(),
                          list(text.hashtags))
    return fit_to_rules(signed, rules, merge_title=False)


def create_post(video_path: Path, template: PlatformText, accounts: list[Account], run_at: datetime | None,
                dry_run: bool, category: str = "", texts: dict[str, PlatformText] | None = None,
                group_id: int | None = None) -> Post:
    """Create a Post and one Job per account. texts: per-platform texts (else cut from the template)."""
    platforms = load_platforms()
    texts = texts or {}
    with get_session() as s:
        post = Post(video_path=str(video_path), title=template.title, description=template.description,
                    hashtags=" ".join(template.hashtags), category=category, scheduled_at=run_at,
                    dry_run=dry_run, group_id=group_id)
        s.add(post)
        s.commit()
        for acc in accounts:
            rules = platforms[acc.platform]
            text = texts.get(acc.platform) or fit_to_rules(template, rules)
            text = with_signature(text, acc, rules)
            s.add(Job(post_id=post.id, account_id=acc.id, run_at=run_at or utcnow(), dry_run=dry_run,
                      text_json=text.to_json()))
        s.commit()
    return post


def group_accounts(group: AccountGroup) -> list[Account]:
    with get_session() as s:
        return [a for a in s.exec(select(Account)).all() if a.id in group.ids]


def next_free_slot(group: AccountGroup, after: datetime | None = None, days: int = 60) -> datetime | None:
    """Nearest slot time of the group (local timezone) that has no publication of this group yet."""
    if not group.slot_times:
        return None
    tz, after = local_tz(), after or utcnow()
    with get_session() as s:
        taken = {p.scheduled_at for p in s.exec(select(Post).where(Post.group_id == group.id)).all()
                 if p.scheduled_at}
    start = after.astimezone(tz).date()
    for day in range(days):
        date = start + timedelta(days=day)
        for slot in group.slot_times:
            try:
                hour, minute = (int(x) for x in slot.split(":"))
                when = datetime(date.year, date.month, date.day, hour, minute, tzinfo=tz).astimezone(timezone.utc)
            except ValueError:
                continue
            if when > after and when not in taken:
                return when
    return None
