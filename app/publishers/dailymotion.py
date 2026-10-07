"""Dailymotion via the upload page of Dailymotion Studio in the browser."""
from pathlib import Path

from app.publishers.base import BrowserPublisher, PublishResult, button
from app.texts import PlatformText

URLS = {
    "upload": "https://www.dailymotion.com/upload",  # redirects to Studio; verify
    "login": "https://www.dailymotion.com/signin",
    "check": "https://www.dailymotion.com/library",  # verify
}

SELECTORS = {
    "file_input": "input[type='file']",
    "title": "input[name='title']",  # verify
    "description": "textarea[name='description']",  # verify
    "tags": "input[name='tags'], input[placeholder*='tag' i]",  # each tag is confirmed with Enter; verify
    "category": "select[name='channel'], select[name='category']",  # verify
    "publish": button("Publish", "Опублікувати", "Опубликовать", "Publier"),
    "published": "text=/published|опубліковано|опубликовано|publiée/i",  # verify
}


class DailymotionPublisher(BrowserPublisher):
    platform = "dailymotion"
    login_url = URLS["login"]
    check_url = URLS["check"]
    cookie_urls = ("https://www.dailymotion.com",)
    login_cookies = frozenset({"access_token"})  # verify
    login_markers = ("/signin",)

    async def _publish(self, page, video: Path, text: PlatformText, dry_run: bool) -> PublishResult:
        await self.open(page, URLS["upload"])
        await self.upload_file(page, video, SELECTORS["file_input"])
        self.go("fill title and description")
        await self.fill(page, SELECTORS["title"], text.title)
        await self.fill(page, SELECTORS["description"], text.description)
        if text.hashtags and await self.visible(page, SELECTORS["tags"], 2000):
            self.go("add tags")
            tags = page.locator(SELECTORS["tags"]).first
            for tag in text.hashtags:
                await tags.fill(tag)
                await tags.press("Enter")
        category = page.locator(SELECTORS["category"]).first
        if await category.count() and not await category.input_value():
            self.go("choose first category")
            await category.select_option(index=1)
        self.go("wait until upload finishes")
        if not await self.wait_enabled(page, SELECTORS["publish"], self.upload_timeout(video)):
            return PublishResult("failed", error_code="upload", error="Publish button stayed disabled")
        if dry_run:
            return self.dry_run_result()
        self.go("click Publish")
        await self.click(page, SELECTORS["publish"])
        await self.confirm(page, SELECTORS["published"], self.step_timeout)
        return PublishResult("published", url=None)
