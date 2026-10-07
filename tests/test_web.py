import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.db import get_session
from app.i18n import TEXTS, translate
from app.main import app
from app.models import Account, Job


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("app.db.init_db", lambda url=None: None)  # keep the in-memory test DB
    with TestClient(app) as c:
        yield c


def test_all_template_keys_are_translated():
    used = set()
    for f in Path(config.TEMPLATES_DIR).rglob("*.html"):
        used |= set(re.findall(r'\bt\(\s*["\']([\w.]+)["\']', f.read_text(encoding="utf-8")))
    missing = {k for k in used if not k.endswith(".")} - set(TEXTS)
    assert not missing
    # keys built at runtime: "status." ~ job.status, "err." ~ code, "acc.login_state." ~ state
    for status in ("pending", "running", "needs_action", "published", "dry_run", "failed", "cancelled"):
        assert f"status.{status}" in TEXTS
    for state in ("queued", "waiting", "ok", "timeout", "error"):
        assert f"acc.login_state.{state}" in TEXTS
    codes = set()
    for f in Path(config.BASE_DIR, "app").rglob("*.py"):
        codes |= set(re.findall(r'error_code=f?"([\w{}.]+)"', f.read_text(encoding="utf-8")))
    for code in codes:
        if "{e.code}" in code:
            for c in ("login", "captcha", "2fa"):
                assert "err." + code.replace("{e.code}", c) in TEXTS
        else:
            assert f"err.{code}" in TEXTS, code


def test_every_text_has_both_languages():
    for key, pair in TEXTS.items():
        assert len(pair) == 2 and all(pair), key
    assert translate("ru", "nav.posts") == "Публикации"
    assert translate("uk", "nav.posts") == "Публікації"


@pytest.mark.parametrize("path", ["/", "/posts", "/accounts", "/settings"])
def test_pages_render_in_both_languages(client, path):
    assert "Публікації" in client.get(path).text
    client.cookies.set("lang", "ru")
    assert "Публикации" in client.get(path).text


def test_create_scheduled_post_creates_jobs(client):
    with get_session() as s:
        acc = Account(platform="youtube", name="Рецепти", slug="recipes")
        s.add(acc)
        s.commit()
    (config.MEDIA_DIR / "abc_test.mp4").write_bytes(b"x")
    r = client.post("/posts", data={
        "video_name": "abc_test.mp4", "account_ids": [str(acc.id)], "title": "T", "description": "D",
        "hashtags": "#a b", "when": "at", "scheduled_at": "2099-01-01T10:00", "dry_run": "1",
    }, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/posts?created=1"
    with get_session() as s:
        job = s.exec(Job.__table__.select()).first()
    assert job.status == "pending" and job.run_at.year == 2099
    assert "Рецепти" in client.get("/posts").text


def test_secret_is_masked_in_settings(client):
    client.post("/settings", data={"timezone": "Europe/Kyiv", "openai_api_key": "sk-verysecretkey123"})
    page = client.get("/settings").text
    assert "sk-verysecretkey123" not in page and "sk-v…y123" in page
