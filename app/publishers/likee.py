"""Likee via likee.video in the browser.

Likee is mostly a mobile app; if the site does not offer uploading for your account,
this adapter fails with debug files (screenshot shows what the site offers).
"""
from pathlib import Path

from app.publishers.base import BrowserPublisher, PublishResult, button
from app.texts import PlatformText

URLS = {
    "upload": "https://likee.video/upload",  # verify
    "login": "https://likee.video/",  # "Log in" button on the home page
}

SELECTORS = {
    "file_input": "input[type='file']",
    "caption": "textarea, div[contenteditable='true']",  # verify
    "post": button("Post", "Publish", "Upload", "Опублікувати", "Опубликовать"),  # verify
    "posted": "text=/success|posted|опубліковано|опубликовано/i",  # verify
}


class LikeePublisher(BrowserPublisher):
    platform = "likee"
    login_url = URLS["login"]
    check_url = URLS["upload"]
    cookie_urls = ("https://likee.video",)
    login_cookies = frozenset({"uid", "likee_uid"})  # verify
    login_markers = ("/login",)

    async def _publish(self, page, video: Path, text: PlatformText, dry_run: bool) -> PublishResult:
        await self.open(page, URLS["upload"])
        await self.upload_file(page, video, SELECTORS["file_input"])
        self.go("fill caption")
        await self.fill(page, SELECTORS["caption"], text.caption())
        self.go("wait until upload finishes")
        if not await self.wait_enabled(page, SELECTORS["post"], self.upload_timeout(video)):
            return PublishResult("failed", error_code="upload", error="Post button stayed disabled")
        if dry_run:
            return self.dry_run_result()
        await self.submit(page, SELECTORS["post"])
        await self.confirm(page, SELECTORS["posted"], self.step_timeout)
        return PublishResult("published", url=None)
