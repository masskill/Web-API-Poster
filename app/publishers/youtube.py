"""YouTube (regular videos and Shorts) via YouTube Studio in the browser.

Shorts need no special handling: a vertical/square video up to 3 minutes becomes a Short.
Selectors use stable element ids / names of YouTube Studio instead of visible text,
because Studio may be shown in Ukrainian, Russian or English.
When YouTube changes its UI, fix the values below (send debug files from data/debug/<job_id>/).
"""
from pathlib import Path

from playwright.async_api import TimeoutError as PlaywrightTimeout

from app.browser import human_pause
from app.publishers.base import NeedsUserAction, PublishResult
from app.texts import PlatformText

URLS = {
    "upload": "https://www.youtube.com/upload",  # redirects to the Studio upload dialog
    "login": "https://accounts.google.com/ServiceLogin?service=youtube&continue=https%3A%2F%2Fstudio.youtube.com%2F",
    "check": "https://studio.youtube.com/",
}

SELECTORS = {
    "file_input": "input[type='file']",
    "title": "#title-textarea #textbox",
    "description": "#description-textarea #textbox",
    "not_for_kids": "tp-yt-paper-radio-button[name='VIDEO_MADE_FOR_KIDS_NOT_MFK']",
    "next": "#next-button",
    "public": "tp-yt-paper-radio-button[name='PUBLIC']",
    "done": "#done-button",  # the final "Publish" button
    "video_link": ".video-url-fadeable a, ytcp-video-info a",  # verify
    "progress": "ytcp-video-upload-progress",  # its text shows "...45%..." while uploading; verify
    "published_close": "ytcp-video-share-dialog #close-button",  # verify
    "captcha": "iframe[src*='recaptcha'], form#captcha-form",
}

# Cookies that exist only for a logged-in YouTube session.
LOGIN_COOKIES = {"SAPISID", "__Secure-3PAPISID", "LOGIN_INFO"}

STEP_TIMEOUT = 60_000  # ms, waiting for an element
UPLOAD_MIN_WAIT = 10 * 60  # s; grows with file size
FORBIDDEN_CHARS = str.maketrans("", "", "<>")  # YouTube rejects < and > in title/description


class YouTubePublisher:
    platform = "youtube"
    uses_browser = True
    login_url = URLS["login"]
    check_url = URLS["check"]

    def __init__(self, account, log):
        self.account = account
        self.log = log
        self.step = ""

    async def is_logged_in(self, page) -> bool:
        if "accounts.google.com" in page.url:
            return False
        cookies = await page.context.cookies(["https://www.youtube.com", "https://studio.youtube.com"])
        return any(c["name"] in LOGIN_COOKIES for c in cookies)

    async def _check_blockers(self, page) -> None:
        """Raise NeedsUserAction for login / 2FA / captcha pages."""
        if "accounts.google.com" in page.url:
            raise NeedsUserAction("2fa" if "/challenge" in page.url else "login")
        if await page.locator(SELECTORS["captcha"]).count():
            async def captcha_gone() -> bool:
                return await page.locator(SELECTORS["captcha"]).count() == 0
            raise NeedsUserAction("captcha", check=captcha_gone)

    def _go(self, step: str) -> None:
        self.step = step
        self.log(step)

    async def _fill(self, box, value: str) -> None:
        await box.click()
        await box.fill(value.translate(FORBIDDEN_CHARS))

    async def publish(self, page, video: Path, text: PlatformText, dry_run: bool) -> PublishResult:
        try:
            return await self._publish(page, video, text, dry_run)
        except PlaywrightTimeout as e:
            await self._check_blockers(page)  # a login page may have appeared mid-way
            return PublishResult("failed", error_code="selector",
                                 error=f"step '{self.step}': {str(e).splitlines()[0]}")

    async def _publish(self, page, video: Path, text: PlatformText, dry_run: bool) -> PublishResult:
        self._go("open upload page")
        await page.goto(URLS["upload"], wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        await self._check_blockers(page)

        self._go("select video file")
        await page.locator(SELECTORS["file_input"]).first.wait_for(state="attached", timeout=STEP_TIMEOUT)
        await human_pause()
        await page.locator(SELECTORS["file_input"]).first.set_input_files(str(video))

        self._go("fill title")
        title_box = page.locator(SELECTORS["title"])
        await title_box.wait_for(state="visible", timeout=STEP_TIMEOUT)
        await human_pause()
        await self._fill(title_box, text.title)

        self._go("fill description")
        await human_pause()
        await self._fill(page.locator(SELECTORS["description"]), text.caption())

        self._go("audience: not made for kids")
        await human_pause()
        await page.locator(SELECTORS["not_for_kids"]).click()

        for i in range(3):  # Details -> Video elements -> Checks -> Visibility
            self._go(f"next ({i + 1}/3)")
            await human_pause()
            await page.locator(SELECTORS["next"]).click()

        self._go("visibility: public")
        await human_pause()
        await page.locator(SELECTORS["public"]).click()

        url = await self._video_url(page)
        self.log(f"video url: {url or 'not found'}")
        if dry_run:
            self.log("dry-run: stopped before the final Publish button "
                     "(YouTube keeps an unpublished draft; delete it in Studio if needed)")
            return PublishResult("dry_run", url=url)

        self._go("wait until upload finishes")
        if not await self._wait_upload(page, video):
            return PublishResult("failed", error_code="upload", error="upload did not finish in time", url=url)

        self._go("click Publish")
        await human_pause()
        await page.locator(SELECTORS["done"]).click()
        try:
            close = page.locator(SELECTORS["published_close"])
            await close.wait_for(state="visible", timeout=STEP_TIMEOUT)
            await close.click()
            self.log("publish confirmed")
        except PlaywrightTimeout:
            self.log("publish confirmation dialog not detected; check the video in Studio")
        return PublishResult("published", url=url)

    async def _video_url(self, page) -> str | None:
        link = page.locator(SELECTORS["video_link"]).first
        try:
            await link.wait_for(state="attached", timeout=30_000)
            return await link.get_attribute("href")
        except PlaywrightTimeout:
            return None

    async def _wait_upload(self, page, video: Path) -> bool:
        """While uploading, the progress label contains a percentage (in any UI language)."""
        size_mb = video.stat().st_size / 1024 / 1024
        deadline_s = UPLOAD_MIN_WAIT + size_mb * 2
        waited = 0
        while waited < deadline_s:
            label = page.locator(SELECTORS["progress"]).first
            text = (await label.inner_text()) if await label.count() else ""
            if "%" not in text:
                return True
            if waited % 60 == 0:
                self.log(f"uploading: {' '.join(text.split())[:80]}")
            await page.wait_for_timeout(5000)
            waited += 5
        return False
