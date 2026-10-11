"""Content plan: posting slots, inbox folder, schedule page."""
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app import automation, config, settings_store
from app.db import get_session
from app.main import app
from app.models import Account, AccountGroup, Job, Post
from app.posting import create_post, next_free_slot
from app.texts import PlatformText

KYIV = ZoneInfo("Europe/Kyiv")


def make_group(slots="10:00, 18:00", platforms=("youtube", "telegram")):
    with get_session() as s:
        accs = [Account(platform=p, name=p, slug=p) for p in platforms]
        for a in accs:
            s.add(a)
        s.commit()
        group = AccountGroup(name="Рецепти", slug="retsepty", account_ids=json.dumps([a.id for a in accs]), slots=slots)
        s.add(group)
        s.commit()
        return group, accs


def test_next_free_slot_skips_past_and_taken():
    settings_store.set_value("timezone", "Europe/Kyiv")
    group, accs = make_group()
    now = datetime(2026, 10, 8, 12, 0, tzinfo=KYIV).astimezone(timezone.utc)
    first = next_free_slot(group, after=now)
    assert first.astimezone(KYIV) == datetime(2026, 10, 8, 18, 0, tzinfo=KYIV)
    video = config.MEDIA_DIR / "v.mp4"
    config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"0")
    create_post(video, PlatformText("T"), accs, first, True, group_id=group.id)
    second = next_free_slot(group, after=now)
    assert second.astimezone(KYIV) == datetime(2026, 10, 9, 10, 0, tzinfo=KYIV)


def test_no_slots_means_none():
    group, _ = make_group(slots="")
    assert next_free_slot(group) is None


def test_parse_sidecar():
    text = automation.parse_sidecar("\nБорщ за 30 хвилин\nСмачний рецепт.\nДругий рядок.\n#борщ #рецепт\n")
    assert text.title == "Борщ за 30 хвилин"
    assert text.description == "Смачний рецепт.\nДругий рядок."
    assert text.hashtags == ["борщ", "рецепт"]


def test_read_text_file_ansi(tmp_path):
    f = tmp_path / "a.txt"
    f.write_bytes("Привіт".encode("cp1251"))
    assert automation.read_text_file(f) == "Привіт"


def test_scan_inbox_imports_stable_video_into_slot(tmp_path, monkeypatch):
    settings_store.set_value("inbox_dir", str(tmp_path / "inbox"))
    settings_store.set_value("dry_run", "1")
    monkeypatch.setattr("app.automation.notify", lambda text, transport=None: _none())
    group, accs = make_group()
    folder = tmp_path / "inbox" / "retsepty"
    folder.mkdir(parents=True)
    (folder / "clip.mp4").write_bytes(b"video")
    (folder / "clip.txt").write_text("Назва\nОпис\n#тег", encoding="utf-8")
    woke = []
    assert automation.scan_inbox(on_created=woke.append) == []  # first sight: maybe still copying
    created = automation.scan_inbox(on_created=woke.append)
    assert len(created) == 1 and woke
    post = created[0]
    assert post.title == "Назва" and post.group_id == group.id and post.scheduled_at is not None
    assert not (folder / "clip.mp4").exists() and list((folder / "_done").glob("*.txt"))
    with get_session() as s:
        jobs = s.exec(select(Job).where(Job.post_id == post.id)).all()
    assert len(jobs) == 2 and all(j.dry_run for j in jobs)


async def _none():
    return None


def test_scan_inbox_skips_group_without_accounts(tmp_path):
    settings_store.set_value("inbox_dir", str(tmp_path / "inbox"))
    with get_session() as s:
        s.add(AccountGroup(name="Empty", slug="empty", account_ids="[]"))
        s.commit()
    folder = tmp_path / "inbox" / "empty"
    folder.mkdir(parents=True)
    (folder / "a.mp4").write_bytes(b"v")
    automation.scan_inbox()
    assert automation.scan_inbox() == [] and (folder / "a.mp4").exists()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("app.db.init_db", lambda url=None: None)
    with TestClient(app) as c:
        yield c


def test_slot_option_and_schedule_page(client):
    group, accs = make_group()
    config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    (config.MEDIA_DIR / "s.mp4").write_bytes(b"0")
    r = client.post("/posts", data={"video_name": "s.mp4", "account_ids": [str(a.id) for a in accs],
                                    "title": "Слот", "when": "slot", "slot_group": str(group.id), "dry_run": "1"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/posts?created=1"
    with get_session() as s:
        post = s.exec(select(Post)).first()
    assert post.group_id == group.id and post.scheduled_at.astimezone(KYIV).strftime("%H:%M") in ("10:00", "18:00")
    page = client.get("/schedule").text
    assert "Слот" in page and "Розклад публікацій" in page
    r = client.post("/posts", data={"video_name": "s.mp4", "account_ids": [str(accs[0].id)], "when": "slot",
                                    "slot_group": "999"}, follow_redirects=False)
    assert r.headers["location"] == "/?error=post.err_no_slots"
