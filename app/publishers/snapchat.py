"""Snapchat Spotlight via the Snapchat web uploader (my.snapchat.com) in the browser."""
from pathlib import Path

from app.publishers.base import BrowserPublisher, PublishResult, button
from app.texts import PlatformText

URLS = {
    "upload": "https://my.snapchat.com/",  # web uploader; verify
    "login": "https://accounts.snapchat.com/accounts/v2/login?continue=https%3A%2F%2Fmy.snapchat.com%2F",
}

SELECTORS = {
    "file_input": "input[type='file']",
    "spotlight": "role=checkbox[name=/Spotlight/i], role=radio[name=/Spotlight/i], "
                 "role=button[name=/Spotlight/i]",  # verify
    "description": "textarea, div[contenteditable='true']",  # verify
    "post": button("Post", "Post to Snapchat", "Submit", "Опублікувати", "Опубликовать"),  # verify
    "posted": "text=/posted|submitted|опубліковано|опубликовано/i",  # verify
}


class SnapchatPublisher(BrowserPublisher):
    platform = "snapchat"
    login_url = URLS["login"]
    check_url = URLS["upload"]
    cookie_urls = ("https://my.snapchat.com", "https://www.snapchat.com", "https://accounts.snapchat.com")
    login_cookies = frozenset({"__Host-sc-a-auth-session", "sc-a-session"})  # verify
    login_markers = ("accounts.snapchat.com",)

    async def _publish(self, page, video: Path, text: PlatformText, dry_run: bool) -> PublishResult:
        await self.open(page, URLS["upload"])
        await self.upload_file(page, video, SELECTORS["file_input"])
        self.go("choose Spotlight")
        spotlight = page.locator(SELECTORS["spotlight"]).first
        await spotlight.wait_for(state="visible", timeout=self.step_timeout)
        if await spotlight.get_attribute("aria-checked") != "true":
            await self.click(page, SELECTORS["spotlight"])
        self.go("fill description")
        await self.fill(page, SELECTORS["description"], text.caption())
        self.go("wait until upload finishes")
        if not await self.wait_enabled(page, SELECTORS["post"], self.upload_timeout(video)):
            return PublishResult("failed", error_code="upload", error="Post button stayed disabled")
        if dry_run:
            return self.dry_run_result()
        await self.submit(page, SELECTORS["post"])
        await self.confirm(page, SELECTORS["posted"], self.step_timeout)
        return PublishResult("published", url=None)
