import asyncio
import importlib
import os

import pytest

from app import settings_store
from app.models import Account
from app.platforms import load_platforms
from app.publishers import PUBLISHERS
from app.publishers.base import BrowserPublisher, NeedsUserAction, PublishResult
from app.texts import PlatformText

BROWSER_ADAPTERS = {k: cls for k, cls in PUBLISHERS.items() if cls.uses_browser}


class FakeLocator:
    first = property(lambda self: self)

    async def count(self):
        return 0

    async def is_visible(self):
        return False


class FakeContext:
    def __init__(self, cookies):
        self._cookies = cookies

    async def cookies(self, urls=None):
        return self._cookies


class FakePage:
    def __init__(self, url, cookies=()):
        self.url = url
        self.context = FakeContext([{"name": n, "value": v} for n, v in cookies])

    def locator(self, selector):
        return FakeLocator()

    async def evaluate(self, script):
        return False


def make(cls):
    return cls(Account(platform=cls.platform, name="A", slug="a"), lambda m: None)


def test_registry_matches_platforms_yaml():
    implemented = {k for k, v in load_platforms().items() if v.get("implemented")}
    assert set(PUBLISHERS) == implemented


@pytest.mark.parametrize("key", sorted(BROWSER_ADAPTERS))
def test_adapter_has_urls_and_selectors(key):
    cls = BROWSER_ADAPTERS[key]
    assert cls.platform == key
    assert cls.login_url.startswith("https://") and cls.check_url.startswith("https://")
    module = importlib.import_module(cls.__module__)
    assert module.SELECTORS and all(isinstance(v, str) and v for v in module.SELECTORS.values())


@pytest.mark.parametrize("key", sorted(k for k, c in BROWSER_ADAPTERS.items() if c.login_cookies))
def test_login_detection_by_cookie(key):
    pub = make(BROWSER_ADAPTERS[key])
    home = pub.check_url
    cookie = sorted(pub.login_cookies)[0]
    assert asyncio.run(pub.is_logged_in(FakePage(home, [(cookie, "1")])))
    assert not asyncio.run(pub.is_logged_in(FakePage(home, [("other", "x")])))
    login_page = pub.login_url if pub._on_login_page(pub.login_url) else None
    if login_page:
        assert not asyncio.run(pub.is_logged_in(FakePage(login_page, [(cookie, "1")])))


@pytest.mark.parametrize("key", sorted(BROWSER_ADAPTERS))
def test_login_page_asks_user(key):
    pub = make(BROWSER_ADAPTERS[key])
    if not pub.login_markers:
        pytest.skip("login is detected without URL")
    with pytest.raises(NeedsUserAction) as e:
        asyncio.run(pub.check_blockers(FakePage("https://example.com" + pub.login_markers[0] + "?x=1")))
    assert e.value.code in ("login", "2fa")


def test_challenge_page_is_2fa():
    pub = make(BROWSER_ADAPTERS["instagram"])
    with pytest.raises(NeedsUserAction) as e:
        asyncio.run(pub.check_blockers(FakePage("https://www.instagram.com/challenge/123/")))
    assert e.value.code == "2fa"


# ---- shared browser steps on a local page (runs only where Chromium can start) ----

PAGE = """<html><body>
<input type="file" id="f" onchange="document.getElementById('post').disabled=false">
<div id="cap" contenteditable="true">old file name</div>
<button id="post" disabled onclick="document.body.insertAdjacentHTML('beforeend','<p>Posted!</p>')">Опублікувати</button>
</body></html>"""


class LocalPublisher(BrowserPublisher):
    platform = "local"

    async def is_logged_in(self, page):
        return True

    async def _publish(self, page, video, text, dry_run):
        await page.set_content(PAGE)
        await self.upload_file(page, video, "#f")
        await self.fill(page, "#cap", text.caption())
        assert await self.wait_enabled(page, "role=button[name=/^(Post|Опублікувати)$/i]", 5000)
        if dry_run:
            return self.dry_run_result()
        await self.click(page, "#post")
        await self.confirm(page, "text=Posted!", 5000)
        return PublishResult("published")


def run_local(tmp_path, dry_run):
    from playwright.async_api import async_playwright

    settings_store.set_value("action_pause_min", "0")
    settings_store.set_value("action_pause_max", "0")
    video = tmp_path / "v.mp4"
    video.write_bytes(b"0")
    logs = []

    async def go():
        async with async_playwright() as pw:
            try:
                b = await pw.chromium.launch(executable_path=os.getenv("BROWSER_EXECUTABLE") or None)
            except Exception as e:
                pytest.skip(f"no browser: {e}")
            page = await b.new_page()
            pub = LocalPublisher(Account(platform="local", name="A", slug="a"), logs.append)
            result = await pub.publish(page, video, PlatformText(description="Борщ", hashtags=["їжа"]), dry_run)
            caption = await page.inner_text("#cap")
            posted = await page.locator("text=Posted!").count()
            await b.close()
            return result, caption, posted

    return asyncio.run(go()), logs


def test_browser_steps_dry_run(tmp_path):
    (result, caption, posted), logs = run_local(tmp_path, True)
    assert result.status == "dry_run" and posted == 0
    assert caption == "Борщ\n\n#їжа" or caption.replace("\n", "") == "Борщ#їжа"


def test_browser_steps_publish(tmp_path):
    (result, caption, posted), logs = run_local(tmp_path, False)
    assert result.status == "published" and posted == 1 and "publish confirmed" in logs
