"""VK Video via vk.com/video in the browser."""
from pathlib import Path

from app.publishers.base import BrowserPublisher, PublishResult, button
from app.texts import PlatformText

URLS = {
    "video": "https://vk.com/video",
    "login": "https://vk.com/login",
    "check": "https://vk.com/feed",
}

SELECTORS = {
    "upload_button": button("Загрузить видео", "Добавить видео", "Завантажити відео", "Додати відео",
                            "Upload video", "Add video"),  # verify
    "file_input": "input[type='file']",
    "title": "input[name='title'], textarea[name='title']",  # verify
    "description": "textarea[name='description'], div[contenteditable='true']",  # verify
    "publish": button("Опубликовать", "Опублікувати", "Publish", "Готово", "Done", "Сохранить", "Зберегти"),
    "video_link": "a[href*='/video-'], a[href*='/video']",  # verify
}


class VKPublisher(BrowserPublisher):
    platform = "vk"
    login_url = URLS["login"]
    check_url = URLS["check"]
    cookie_urls = ("https://vk.com", "https://vk.ru")
    login_cookies = frozenset({"remixsid"})
    login_markers = ("/login", "id.vk.com", "oauth.vk.com")

    async def _publish(self, page, video: Path, text: PlatformText, dry_run: bool) -> PublishResult:
        await self.open(page, URLS["video"])
        self.go("open upload dialog")
        await self.click(page, SELECTORS["upload_button"])
        await self.upload_file(page, video, SELECTORS["file_input"])
        self.go("fill title and description")
        await self.fill(page, SELECTORS["title"], text.title)
        if await self.visible(page, SELECTORS["description"], 3000):
            await self.fill(page, SELECTORS["description"], text.caption())
        self.go("wait until upload finishes")
        if not await self.wait_enabled(page, SELECTORS["publish"], self.upload_timeout(video)):
            return PublishResult("failed", error_code="upload", error="Publish button stayed disabled")
        if dry_run:
            return self.dry_run_result()
        self.go("click Publish")
        await self.click(page, SELECTORS["publish"])
        await page.wait_for_timeout(3000)
        return PublishResult("published", url=None)
