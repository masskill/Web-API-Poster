"""Bluesky via bsky.app in the browser.

Bluesky keeps its session in localStorage (not in cookies), so login is detected there.
"""
from pathlib import Path

from app.publishers.base import BrowserPublisher, PublishResult
from app.texts import PlatformText

URLS = {
    "home": "https://bsky.app/",
    "login": "https://bsky.app/",  # the "Sign in" button is on the home page
}

SELECTORS = {
    "compose": "[data-testid='composeFAB'], button[aria-label='New post'], button[aria-label='Новий пост'], "
               "button[aria-label='Новый пост']",  # verify
    "text": "[data-testid='composerTextInput'] [contenteditable='true'], div.ProseMirror[contenteditable='true']",
    "media": "[data-testid='openMediaBtn']",  # opens the file dialog; verify
    "post": "[data-testid='composerPublishBtn']",
}

SESSION_JS = "() => { const s = localStorage.getItem('BSKY_STORAGE'); return !!s && s.includes('accessJwt'); }"  # verify


class BlueskyPublisher(BrowserPublisher):
    platform = "bluesky"
    login_url = URLS["login"]
    check_url = URLS["home"]
    login_markers = ()

    async def is_logged_in(self, page) -> bool:
        if "bsky.app" not in page.url:
            return False
        return bool(await page.evaluate(SESSION_JS))

    async def _publish(self, page, video: Path, text: PlatformText, dry_run: bool) -> PublishResult:
        await self.open(page, URLS["home"])
        self.go("open composer")
        await self.click(page, SELECTORS["compose"])
        self.go("fill text")
        await self.fill(page, SELECTORS["text"], text.caption())
        await self.upload_file(page, video, chooser_button=SELECTORS["media"])
        self.go("wait until video is processed")
        if not await self.wait_enabled(page, SELECTORS["post"], self.upload_timeout(video)):
            return PublishResult("failed", error_code="upload", error="Post button stayed disabled")
        if dry_run:
            return self.dry_run_result()
        await self.submit(page, SELECTORS["post"])
        await self.confirm(page, SELECTORS["text"], self.upload_timeout(video), state="hidden")
        return PublishResult("published", url=None)
