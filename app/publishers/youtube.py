"""YouTube (regular videos and Shorts) via YouTube Studio in the browser.

Shorts need no special handling: a vertical/square video up to 3 minutes becomes a Short.
Selectors use stable element ids / names of YouTube Studio instead of visible text,
because Studio may be shown in Ukrainian, Russian or English.
When YouTube changes its UI, fix the values below (send debug files from data/debug/<job_id>/).
"""
from pathlib import Path

from app.publishers.base import BrowserPublisher, PublishResult
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
}

FORBIDDEN_CHARS = str.maketrans("", "", "<>")  # YouTube rejects < and > in title/description


class YouTubePublisher(BrowserPublisher):
    platform = "youtube"
    login_url = URLS["login"]
    check_url = URLS["check"]
    cookie_urls = ("https://www.youtube.com", "https://studio.youtube.com")
    login_cookies = frozenset({"SAPISID", "__Secure-3PAPISID", "LOGIN_INFO"})
    login_markers = ("accounts.google.com",)
    challenge_markers = ("/challenge",)

    async def _publish(self, page, video: Path, text: PlatformText, dry_run: bool) -> PublishResult:
        await self.open(page, URLS["upload"])
        await self.upload_file(page, video, SELECTORS["file_input"])

        self.go("fill title")
        await self.fill(page, SELECTORS["title"], text.title.translate(FORBIDDEN_CHARS))
        self.go("fill description")
        await self.fill(page, SELECTORS["description"], text.caption().translate(FORBIDDEN_CHARS))

        self.go("audience: not made for kids")
        await self.click(page, SELECTORS["not_for_kids"])
        for i in range(3):  # Details -> Video elements -> Checks -> Visibility
            self.go(f"next ({i + 1}/3)")
            await self.click(page, SELECTORS["next"])
        self.go("visibility: public")
        await self.click(page, SELECTORS["public"])

        url = await self.href(page, SELECTORS["video_link"], timeout=30_000)
        self.log(f"video url: {url or 'not found'}")
        if dry_run:
            self.log("YouTube keeps an unpublished draft; delete it in Studio if needed")
            return self.dry_run_result(url)

        self.go("wait until upload finishes")
        if not await self._wait_upload(page, video):
            return PublishResult("failed", error_code="upload", error="upload did not finish in time", url=url)
        await self.submit(page, SELECTORS["done"])
        if await self.confirm(page, SELECTORS["published_close"], self.step_timeout):
            await self.click(page, SELECTORS["published_close"])
        return PublishResult("published", url=url)

    async def _wait_upload(self, page, video: Path) -> bool:
        """While uploading, the progress label contains a percentage (in any UI language)."""
        waited, limit = 0, self.upload_timeout(video)
        while waited < limit:
            label = page.locator(SELECTORS["progress"]).first
            text = (await label.inner_text()) if await label.count() else ""
            if "%" not in text:
                return True
            if waited % 60_000 == 0:
                self.log(f"uploading: {' '.join(text.split())[:80]}")
            await page.wait_for_timeout(5000)
            waited += 5000
        return False
