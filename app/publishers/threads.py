"""Threads via threads.com in the browser (login with the Instagram account)."""
from pathlib import Path

from app.publishers.base import BrowserPublisher, PublishResult, button
from app.texts import PlatformText

URLS = {
    "home": "https://www.threads.com/",
    "login": "https://www.threads.com/login",
}

DIALOG = "div[role='dialog']"
SELECTORS = {
    "create": "svg[aria-label='Create'], svg[aria-label='Створити'], svg[aria-label='Создать']",  # verify
    "text": f"{DIALOG} div[contenteditable='true']",
    "file_input": f"{DIALOG} input[type='file']",
    "post": f"{DIALOG} >> " + button("Post", "Опублікувати", "Опубликовать"),
}


class ThreadsPublisher(BrowserPublisher):
    platform = "threads"
    login_url = URLS["login"]
    check_url = URLS["home"]
    cookie_urls = ("https://www.threads.com",)
    login_cookies = frozenset({"sessionid"})

    async def _publish(self, page, video: Path, text: PlatformText, dry_run: bool) -> PublishResult:
        await self.open(page, URLS["home"])
        self.go("open composer")
        await self.click(page, SELECTORS["create"])
        self.go("fill text")
        await self.fill(page, SELECTORS["text"], text.caption())
        await self.upload_file(page, video, SELECTORS["file_input"])
        self.go("wait until video is uploaded")
        if not await self.wait_enabled(page, SELECTORS["post"], self.upload_timeout(video)):
            return PublishResult("failed", error_code="upload", error="Post button stayed disabled")
        if dry_run:
            return self.dry_run_result()
        await self.submit(page, SELECTORS["post"])
        await self.confirm(page, SELECTORS["text"], self.upload_timeout(video), state="hidden")
        return PublishResult("published", url=None)
