"""Vimeo via vimeo.com/upload in the browser.

On Vimeo a video goes live as soon as the file is uploaded (with the account's default privacy),
so dry-run only checks login and the upload page and does NOT upload the file.
"""
import re
from pathlib import Path

from app.publishers.base import BrowserPublisher, PublishResult, button
from app.texts import PlatformText

URLS = {
    "upload": "https://vimeo.com/upload/videos",
    "login": "https://vimeo.com/log_in",
    "check": "https://vimeo.com/manage/videos",
}

SELECTORS = {
    "file_input": "input[type='file']",
    "manage_link": "a[href*='/manage/videos/']",  # link to the uploaded video; verify
    "title": "input[name='title'], input[aria-label='Title' i], input[aria-label='Назва' i], "
             "input[aria-label='Название' i]",  # verify
    "description": "textarea[name='description'], textarea[aria-label*='escription' i]",  # verify
    "save": button("Save", "Зберегти", "Сохранить"),
}


class VimeoPublisher(BrowserPublisher):
    platform = "vimeo"
    login_url = URLS["login"]
    check_url = URLS["check"]
    cookie_urls = ("https://vimeo.com",)
    login_cookies = frozenset({"vimeo"})
    login_markers = ("/log_in",)

    async def _publish(self, page, video: Path, text: PlatformText, dry_run: bool) -> PublishResult:
        await self.open(page, URLS["upload"])
        await page.locator(SELECTORS["file_input"]).first.wait_for(state="attached", timeout=self.step_timeout)
        if dry_run:
            self.log("Vimeo publishes right after upload, so dry-run does not upload the file")
            return self.dry_run_result()
        await self.upload_file(page, video, SELECTORS["file_input"])
        self.submitted = True  # on Vimeo the upload itself publishes the video
        self.go("wait for the video link")
        link = await self.href(page, SELECTORS["manage_link"], "https://vimeo.com", self.upload_timeout(video))
        if not link:
            return PublishResult("published", error_code="unconfirmed", error="uploaded video link not found")
        match = re.search(r"/videos/(\d+)", link)
        url = f"https://vimeo.com/{match.group(1)}" if match else None
        self.go("open video settings")
        await page.goto(link, wait_until="domcontentloaded")
        await self.fill(page, SELECTORS["title"], text.title)
        if await self.visible(page, SELECTORS["description"], 3000):
            await self.fill(page, SELECTORS["description"], text.caption())
        self.go("click Save")
        await self.click(page, SELECTORS["save"])
        return PublishResult("published", url=url)
