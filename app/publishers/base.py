"""Common adapter interface. Every platform adapter follows the Publisher protocol."""
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Protocol

from playwright.async_api import TimeoutError as PlaywrightTimeout

from app.browser import human_pause
from app.texts import PlatformText

LogFn = Callable[[str], None]


@dataclass
class PublishResult:
    status: str  # published | dry_run | failed
    url: str | None = None
    error: str | None = None
    error_code: str | None = None
    screenshot: str | None = None


class NeedsUserAction(Exception):
    """Captcha, 2FA or login is required.

    code: "login" | "captcha" | "2fa".
    check: async callable that returns True once the user has resolved it
    (None means "wait until is_logged_in() is True").
    """

    def __init__(self, code: str, check: Callable[[], Awaitable[bool]] | None = None):
        super().__init__(code)
        self.code = code
        self.check = check


class Publisher(Protocol):
    platform: str
    uses_browser: bool
    login_url: str  # opened by the "Увійти" button
    check_url: str  # opened by "Перевірити вхід"

    def __init__(self, account, log: LogFn): ...

    async def is_logged_in(self, page) -> bool: ...

    async def publish(self, page, video: Path, text: PlatformText, dry_run: bool) -> PublishResult: ...


def button(*names: str) -> str:
    """Selector for a button by its visible name in any of the given languages (uk / ru / en)."""
    return "role=button[name=/^\\s*(" + "|".join(names) + ")\\s*$/i]"


class BrowserPublisher:
    """Shared steps of browser adapters. A subclass sets URLs / login cookies and implements _publish()."""

    platform = ""
    uses_browser = True
    login_url = ""
    check_url = ""
    cookie_urls: tuple[str, ...] = ()
    login_cookies: frozenset[str] = frozenset()  # cookie names that exist only when logged in
    login_markers: tuple[str, ...] = ("/login",)  # URL parts of login pages
    challenge_markers: tuple[str, ...] = ("/challenge", "checkpoint", "two_factor", "two_step")
    captcha_selector = "iframe[src*='captcha' i], iframe[title*='captcha' i], #captcha-verify-image"
    step_timeout = 60_000  # ms

    def __init__(self, account, log: LogFn):
        self.account = account
        self.log = log
        self.step = ""

    # ---- login / blockers ----

    def _on_login_page(self, url: str) -> bool:
        return any(m in url for m in self.login_markers + self.challenge_markers)

    async def is_logged_in(self, page) -> bool:
        if self._on_login_page(page.url):
            return False
        cookies = await page.context.cookies(list(self.cookie_urls))
        return any(c["name"] in self.login_cookies and c["value"] for c in cookies)

    async def check_blockers(self, page) -> None:
        """Raise NeedsUserAction for a 2FA / login page or a visible captcha."""
        if any(m in page.url for m in self.challenge_markers):
            raise NeedsUserAction("2fa")
        if any(m in page.url for m in self.login_markers):
            raise NeedsUserAction("login")
        captcha = page.locator(self.captcha_selector).first
        if await captcha.count() and await captcha.is_visible():
            async def captcha_gone() -> bool:
                c = page.locator(self.captcha_selector).first
                return not (await c.count() and await c.is_visible())
            raise NeedsUserAction("captcha", check=captcha_gone)

    async def open(self, page, url: str) -> None:
        """Open a page of the platform; if the session is gone, show the login page and ask the user."""
        self.go(f"open {url}")
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)
        await self.check_blockers(page)
        if not await self.is_logged_in(page):
            await page.goto(self.login_url, wait_until="domcontentloaded")
            raise NeedsUserAction("login")

    # ---- publishing ----

    async def publish(self, page, video: Path, text: PlatformText, dry_run: bool) -> PublishResult:
        try:
            return await self._publish(page, video, text, dry_run)
        except PlaywrightTimeout as e:
            await self.check_blockers(page)  # a login page or captcha may have appeared mid-way
            return PublishResult("failed", error_code="selector",
                                 error=f"step '{self.step}': {str(e).splitlines()[0]}")

    async def _publish(self, page, video: Path, text: PlatformText, dry_run: bool) -> PublishResult:
        raise NotImplementedError

    # ---- helpers for subclasses ----

    def go(self, step: str) -> None:
        self.step = step
        self.log(step)

    def upload_timeout(self, video: Path) -> int:
        """ms to wait for an upload: 10 minutes plus 2 s per MB."""
        return int((600 + video.stat().st_size / 1024 / 1024 * 2) * 1000)

    async def visible(self, page, selector: str, timeout: int = 5000) -> bool:
        try:
            await page.locator(selector).first.wait_for(state="visible", timeout=timeout)
            return True
        except Exception:
            return False

    async def click(self, page, selector: str, timeout: int | None = None) -> None:
        loc = page.locator(selector).first
        await loc.wait_for(state="visible", timeout=timeout or self.step_timeout)
        await human_pause()
        await loc.click(timeout=timeout or self.step_timeout)

    async def click_if_visible(self, page, selector: str, timeout: int = 4000) -> bool:
        if await self.visible(page, selector, timeout):
            await self.click(page, selector)
            return True
        return False

    async def fill(self, page, selector: str, value: str) -> None:
        """Fill an input, a textarea or a rich-text (contenteditable) editor, replacing old text."""
        loc = page.locator(selector).first
        await loc.wait_for(state="visible", timeout=self.step_timeout)
        await human_pause()
        await loc.click()
        if await loc.evaluate("e => e.isContentEditable"):
            await page.keyboard.press("ControlOrMeta+A")
            await page.keyboard.press("Delete")
            await page.keyboard.insert_text(value)
        else:
            await loc.fill(value)

    async def upload_file(self, page, video: Path, input_selector: str = "input[type='file']",
                          chooser_button: str | None = None) -> None:
        """Give the video to a file input, or to the file dialog opened by chooser_button."""
        self.go(f"upload {video.name}")
        if chooser_button:
            async with page.expect_file_chooser(timeout=self.step_timeout) as fc:
                await self.click(page, chooser_button)
            await (await fc.value).set_files(str(video))
            return
        loc = page.locator(input_selector).first
        await loc.wait_for(state="attached", timeout=self.step_timeout)
        await human_pause()
        await loc.set_input_files(str(video))

    async def wait_enabled(self, page, selector: str, timeout_ms: int) -> bool:
        """Wait until a button becomes clickable (platforms disable 'Post' while the video uploads)."""
        loc = page.locator(selector).first
        waited = 0
        while waited < timeout_ms:
            try:
                if (await loc.is_visible() and await loc.is_enabled()
                        and await loc.get_attribute("aria-disabled") != "true"
                        and await loc.get_attribute("data-disabled") != "true"):
                    return True
            except Exception:
                pass
            await page.wait_for_timeout(2000)
            waited += 2000
            if waited % 60_000 == 0:
                self.log(f"still waiting for upload ({waited // 1000} s)")
        return False

    async def href(self, page, selector: str, base: str = "", timeout: int = 15_000) -> str | None:
        try:
            loc = page.locator(selector).first
            await loc.wait_for(state="attached", timeout=timeout)
            link = await loc.get_attribute("href")
        except Exception:
            return None
        if link and link.startswith("/"):
            link = base + link
        return link

    def dry_run_result(self, url: str | None = None) -> PublishResult:
        self.log("dry-run: stopped before the final publish button")
        return PublishResult("dry_run", url=url)

    async def confirm(self, page, selector: str, timeout: int) -> bool:
        """Wait for a sign that the post went out; log if it was not seen."""
        if await self.visible(page, selector, timeout):
            self.log("publish confirmed")
            return True
        self.log("confirmation not detected; check the post on the site")
        return False
