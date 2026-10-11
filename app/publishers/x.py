"""X (Twitter) via x.com compose page in the browser."""
from pathlib import Path

from app.publishers.base import BrowserPublisher, PublishResult
from app.texts import PlatformText

URLS = {
    "compose": "https://x.com/compose/post",
    "login": "https://x.com/i/flow/login",
    "check": "https://x.com/home",
}

SELECTORS = {
    "file_input": "input[data-testid='fileInput']",
    "text": "div[data-testid='tweetTextarea_0']",
    "post": "button[data-testid='tweetButton']",
    "posted_link": "div[data-testid='toast'] a[href*='/status/']",  # "View" link in the toast; verify
}


class XPublisher(BrowserPublisher):
    platform = "x"
    login_url = URLS["login"]
    check_url = URLS["check"]
    cookie_urls = ("https://x.com",)
    login_cookies = frozenset({"auth_token"})
    login_markers = ("/i/flow/login", "/login")
    challenge_markers = ("/account/access", "/i/flow/challenge")

    async def _publish(self, page, video: Path, text: PlatformText, dry_run: bool) -> PublishResult:
        await self.open(page, URLS["compose"])
        self.go("fill text")
        await self.fill(page, SELECTORS["text"], text.caption())
        await self.upload_file(page, video, SELECTORS["file_input"])
        self.go("wait until video is processed")
        if not await self.wait_enabled(page, SELECTORS["post"], self.upload_timeout(video)):
            return PublishResult("failed", error_code="upload", error="Post button stayed disabled")
        if dry_run:
            return self.dry_run_result()
        await self.submit(page, SELECTORS["post"])
        url = await self.href(page, SELECTORS["posted_link"], base="https://x.com")
        self.log(f"post url: {url or 'not found'}")
        return PublishResult("published", url=url)
