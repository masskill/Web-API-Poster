"""Facebook Reels via facebook.com/reels/create in the browser.

To post as a Page, switch the profile to that Page once in the login window
(the choice is kept in this account's browser profile).
"""
from pathlib import Path

from app.publishers.base import BrowserPublisher, PublishResult, button
from app.texts import PlatformText

URLS = {
    "create": "https://www.facebook.com/reels/create",
    "login": "https://www.facebook.com/login",
    "check": "https://www.facebook.com/",
}

SELECTORS = {
    "file_input": "input[type='file'][accept*='video']",
    "next": button("Next", "Далі", "Далее"),
    "description": "div[role='textbox'][contenteditable='true']",  # verify
    "publish": button("Publish", "Опублікувати", "Опубликовать", "Share", "Поширити", "Поделиться"),
    "published": "text=/reel (is being |has been )?published|опубліковано|опубликовано/i",  # verify
}


class FacebookPublisher(BrowserPublisher):
    platform = "facebook"
    login_url = URLS["login"]
    check_url = URLS["check"]
    cookie_urls = ("https://www.facebook.com",)
    login_cookies = frozenset({"c_user"})

    async def _publish(self, page, video: Path, text: PlatformText, dry_run: bool) -> PublishResult:
        await self.open(page, URLS["create"])
        await self.upload_file(page, video, SELECTORS["file_input"])
        for i in range(3):  # upload -> edit -> details (the number of steps varies)
            if await self.visible(page, SELECTORS["description"], 3000):
                break
            self.go(f"next ({i + 1})")
            await self.click(page, SELECTORS["next"])
        self.go("fill description")
        await self.fill(page, SELECTORS["description"], text.caption())
        self.go("wait until upload finishes")
        if not await self.wait_enabled(page, SELECTORS["publish"], self.upload_timeout(video)):
            return PublishResult("failed", error_code="upload", error="Publish button stayed disabled")
        if dry_run:
            return self.dry_run_result()
        self.go("click Publish")
        await self.click(page, SELECTORS["publish"])
        await self.confirm(page, SELECTORS["published"], self.step_timeout)
        return PublishResult("published", url=None)
