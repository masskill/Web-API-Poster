"""Instruction and changelog: safe Markdown rendering, versions, pages, the "new" badge."""
import re

import pytest
from fastapi.testclient import TestClient

from app import config
from app.docs import load_doc, render_markdown
from app.main import app


def test_markdown_basics_and_safety():
    html, contents = render_markdown(
        "# Title\n\n## 1. Встановлення\n\nText with **bold**, `code` and [link](https://example.com).\n\n"
        "- one\n- two\n\n1. first\n\n```\nrun.ps1 <x>\n```\n\n2. second\n\n<script>alert(1)</script>\n"
        "[bad](javascript:alert(1))\n")
    html = str(html)
    assert '<h2 id="1-vstanovlennia">1. Встановлення</h2>' in html
    assert contents == [("1-vstanovlennia", "1. Встановлення")]
    assert "<b>bold</b>" in html and "<code>code</code>" in html
    assert '<a href="https://example.com" target="_blank" rel="noopener">link</a>' in html
    assert "<ul>\n<li>one</li>\n<li>two</li>\n</ul>" in html
    assert '<ol start="2">' in html  # numbering continues after a code block
    assert "<pre class=\"prompt\">run.ps1 &lt;x&gt;</pre>" in html
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "javascript:" not in html


def versions(text: str) -> list[str]:
    return re.findall(r"^## (\d+\.\d+\.\d+)", text, flags=re.M)


def test_changelog_matches_app_version_in_both_languages():
    uk, ru = versions(load_doc("updates", "uk")), versions(load_doc("updates", "ru"))
    assert uk and uk == ru
    assert uk[0] == config.APP_VERSION


def test_instruction_has_same_sections_in_both_languages():
    _, uk = render_markdown(load_doc("guide", "uk"))
    _, ru = render_markdown(load_doc("guide", "ru"))
    assert len(uk) == len(ru) >= 10


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("app.db.init_db", lambda url=None: None)
    with TestClient(app) as c:
        yield c


def test_pages_and_new_badge(client):
    home = client.get("/").text
    assert 'class="badge new"' in home and f"версія {config.APP_VERSION}" in home
    page = client.get("/guide").text
    assert "Інструкція з використання" in page and "Зміст" in page
    page = client.get("/updates").text
    assert config.APP_VERSION in page and 'class="badge new"' not in page
    assert client.cookies.get("seen_version") == config.APP_VERSION
    assert 'class="badge new"' not in client.get("/").text
    client.cookies.set("lang", "ru")
    assert "Инструкция по использованию" in client.get("/guide").text
    assert "Обновления Web-API-Poster" in client.get("/updates").text
