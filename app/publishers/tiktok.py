"""TikTok via TikTok Studio upload page in the browser."""
from pathlib import Path

from app.publishers.base import BrowserPublisher, PublishResult, button
from app.texts import PlatformText

URLS = {
    "upload": "https://www.tiktok.com/tiktokstudio/upload",
    "login": "https://www.tiktok.com/login",
    "check": "https://www.tiktok.com/tiktokstudio",
}

SELECTORS = {
    "file_input": "input[type='file'][accept*='video']",
    "caption": "div.public-DraftEditor-content[contenteditable='true'], div[contenteditable='true']",  # verify
    "post": "button[data-e2e='post_video_button']",
    # dialog "Continue to post?" after content checks; verify
    "post_now": button("Post now", "Опублікувати зараз", "Опубликовать сейчас"),
    "posted": "text=/video (has been|is being) (posted|uploaded)|опубліковано|опубликовано|Manage your posts|Керувати|Управл/i",  # verify
}


class TikTokPublisher(BrowserPublisher):
    platform = "tiktok"
    login_url = URLS["login"]
    check_url = URLS["check"]
    cookie_urls = ("https://www.tiktok.com",)
    login_cookies = frozenset({"sessionid", "sessionid_ss"})

    async def _publish(self, page, video: Path, text: PlatformText, dry_run: bool) -> PublishResult:
        await self.open(page, URLS["upload"])
        await self.upload_file(page, video, SELECTORS["file_input"])
        self.go("fill caption")  # TikTok pre-fills the file name; fill() replaces it
        await self.fill(page, SELECTORS["caption"], text.caption())
        self.go("wait until upload finishes")
        if not await self.wait_enabled(page, SELECTORS["post"], self.upload_timeout(video)):
            return PublishResult("failed", error_code="upload", error="Post button stayed disabled")
        if dry_run:
            return self.dry_run_result()
        self.go("click Post")
        await self.click(page, SELECTORS["post"])
        await self.click_if_visible(page, SELECTORS["post_now"])
        await self.confirm(page, SELECTORS["posted"], self.step_timeout)
        return PublishResult("published", url=None)
