"""Publication management: real publish after dry-run, edit, delete, signature, groups, platform test."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app import config, worker
from app.db import get_session
from app.main import app
from app.models import Account, AccountGroup, Job, Post, utcnow
from app.posting import create_post
from app.texts import PlatformText


def add_account(platform="youtube", name="A", signature=""):
    with get_session() as s:
        acc = Account(platform=platform, name=name, slug=name.lower(), signature=signature)
        s.add(acc)
        s.commit()
        return acc


def media_video(name="v.mp4"):
    config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    path = config.MEDIA_DIR / name
    path.write_bytes(b"0")
    return path


def jobs():
    with get_session() as s:
        return s.exec(select(Job)).all()


def test_signature_is_appended_within_limits():
    acc = add_account("x", "X", signature="Підписуйтесь!")
    create_post(media_video(), PlatformText("T", "D" * 400, ["a"]), [acc], None, True)
    text = PlatformText.from_json(jobs()[0].text_json)
    assert text.description.endswith("Підписуйтесь!") and len(text.caption()) <= 280


def test_publish_for_real_after_dry_run():
    acc = add_account()
    create_post(media_video(), PlatformText("T", "D"), [acc], None, True)
    job = jobs()[0]
    assert job.dry_run and not worker.publish_for_real(job.id)  # still pending: nothing to do
    with get_session() as s:
        j = s.get(Job, job.id)
        j.status = "dry_run"
        s.add(j)
        s.commit()
    assert worker.publish_for_real(job.id)
    job = jobs()[0]
    assert job.status == "pending" and job.dry_run is False


def test_update_job_text_and_time():
    acc = add_account()
    create_post(media_video(), PlatformText("T", "D"), [acc], None, True)
    when = utcnow() + timedelta(days=1)
    assert worker.update_job(jobs()[0].id, PlatformText("New", "Desc", ["x"]), when)
    job = jobs()[0]
    assert PlatformText.from_json(job.text_json).title == "New" and job.run_at == when


def test_delete_post_removes_unused_video():
    acc = add_account()
    video = media_video("only.mp4")
    post = create_post(video, PlatformText("T", "D"), [acc], None, True)
    assert worker.delete_post(post.id)
    assert not video.exists() and jobs() == []


def test_delete_post_keeps_shared_video_and_refuses_running():
    acc = add_account()
    video = media_video("shared.mp4")
    first = create_post(video, PlatformText("T", "D"), [acc], None, True)
    second = create_post(video, PlatformText("T", "D"), [acc], None, True)
    assert worker.delete_post(first.id) and video.exists()
    with get_session() as s:
        job = s.exec(select(Job).where(Job.post_id == second.id)).first()
        job.status = "running"
        s.add(job)
        s.commit()
    assert not worker.delete_post(second.id)


def test_cancelled_running_job_keeps_status_unless_published():
    from app.publishers.base import PublishResult
    acc = add_account()
    create_post(media_video(), PlatformText("T", "D"), [acc], None, False)
    job_id = jobs()[0].id
    assert worker.cancel_job(job_id)
    worker.finish_job(job_id, PublishResult("failed", error_code="exception"), None)
    assert jobs()[0].status == "cancelled"
    worker.finish_job(job_id, PublishResult("published", error_code="unconfirmed"), None)
    assert jobs()[0].status == "published"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("app.db.init_db", lambda url=None: None)
    with TestClient(app) as c:
        yield c


def test_group_and_platform_test_routes(client):
    a, b = add_account("youtube", "A"), add_account("tiktok", "B")
    client.post("/groups", data={"name": "Рецепти", "account_ids": [str(a.id), str(b.id)], "slots": "10:00"})
    with get_session() as s:
        group = s.exec(select(AccountGroup)).first()
    assert group.slug == "retsepty" and group.ids == [a.id, b.id] and group.slot_times == ["10:00"]
    assert "Рецепти" in client.get("/").text
    r = client.post(f"/accounts/{a.id}/test")
    assert r.headers["HX-Redirect"] == "/posts"
    job = jobs()[0]
    assert job.dry_run and job.account_id == a.id


def test_account_edit_and_job_edit_pages(client):
    acc = add_account("telegram", "T")
    client.post(f"/accounts/{acc.id}/edit", data={"name": "News", "signature": "@news", "chat_id": "@c",
                                                  "bot_token": ""})
    with get_session() as s:
        acc = s.get(Account, acc.id)
    assert acc.name == "News" and acc.signature == "@news" and acc.cfg["chat_id"] == "@c"
    create_post(media_video(), PlatformText("T", "D"), [acc], None, True)
    job = jobs()[0]
    page = client.get(f"/jobs/{job.id}/edit")
    assert page.status_code == 200 and "Редагування завдання" in page.text
    client.post(f"/jobs/{job.id}/edit", data={"description": "Edited", "hashtags": "#a", "run_at": "2099-05-01T09:30"})
    job = jobs()[0]
    assert PlatformText.from_json(job.text_json).description == "Edited" and job.run_at.year == 2099
