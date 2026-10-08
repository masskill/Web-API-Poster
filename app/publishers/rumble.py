"""Rumble via rumble.com/upload in the browser (two steps: details, then license and terms)."""
from pathlib import Path

from app.publishers.base import BrowserPublisher, PublishResult, button
from app.texts import PlatformText

URLS = {
    "upload": "https://rumble.com/upload",
    "login": "https://rumble.com/login.php",
}

SELECTORS = {
    "file_input": "input[type='file']",
    "title": "input#title, input[name='title']",  # verify
    "description": "textarea#description, textarea[name='description']",  # verify
    "tags": "input#tags, input[name='tags']",  # verify
    "category": "select#category_primary, select[name='primary-category']",  # verify
    "next": "#submitForm, " + button("Upload", "Continue", "Next", "Далее", "Далі"),  # verify
    "terms": "input[type='checkbox'][name*='crights' i], input[type='checkbox'][name*='terms' i]",  # verify
    "submit": "#submitForm2, " + button("Submit", "Publish", "Опубликовать", "Опублікувати"),  # verify
    "done": "text=/upload complete|successfully|video is processing/i",  # verify
    "video_link": "a[href^='https://rumble.com/v'], a[href^='/v']",  # verify
}


class RumblePublisher(BrowserPublisher):
    platform = "rumble"
    login_url = URLS["login"]
    check_url = URLS["upload"]
    cookie_urls = ("https://rumble.com",)
    login_cookies = frozenset({"u_s"})  # verify
    login_markers = ("login.php", "/login")

    async def _publish(self, page, video: Path, text: PlatformText, dry_run: bool) -> PublishResult:
        await self.open(page, URLS["upload"])
        await self.upload_file(page, video, SELECTORS["file_input"])
        self.go("fill title, description, tags")
        await self.fill(page, SELECTORS["title"], text.title)
        await self.fill(page, SELECTORS["description"], text.description)
        if await self.visible(page, SELECTORS["tags"], 2000):
            await self.fill(page, SELECTORS["tags"], ", ".join(text.hashtags))
        category = page.locator(SELECTORS["category"]).first
        if await category.count() and not await category.input_value():
            self.go("choose first category")
            await category.select_option(index=1)
        self.go("wait until upload finishes")
        if not await self.wait_enabled(page, SELECTORS["next"], self.upload_timeout(video)):
            return PublishResult("failed", error_code="upload", error="Next button stayed disabled")
        self.go("next: license and terms")
        await self.click(page, SELECTORS["next"])
        await page.wait_for_timeout(2000)
        for box in await page.locator(SELECTORS["terms"]).all():
            if await box.is_visible() and not await box.is_checked():
                await box.check()
        if dry_run:
            return self.dry_run_result()
        await self.submit(page, SELECTORS["submit"])
        await self.confirm(page, SELECTORS["done"], self.step_timeout)
        return PublishResult("published", url=await self.href(page, SELECTORS["video_link"], "https://rumble.com", 5000))
