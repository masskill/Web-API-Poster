"""Pinterest video Pin via the Pin creation tool in the browser.

The Pin goes to the board that Pinterest pre-selects (usually the last used one);
if no board is selected, the first board in the list is chosen.
"""
from pathlib import Path

from app.publishers.base import BrowserPublisher, PublishResult, button
from app.texts import PlatformText

URLS = {
    "create": "https://www.pinterest.com/pin-creation-tool/",
    "login": "https://www.pinterest.com/login/",
    "check": "https://www.pinterest.com/",
}

SELECTORS = {
    "file_input": "input[type='file']",
    "title": "input#storyboard-selector-title, input[name='title']",  # verify
    "description": "[data-test-id='storyboard-description-field-container'] [contenteditable='true'], "
                   "div[contenteditable='true'][role='combobox']",  # verify
    "board_dropdown": "[data-test-id='board-dropdown-select-button']",  # verify
    "board_first": "[data-test-id='board-row'], div[role='listbox'] div[role='button']",  # verify
    "publish": "[data-test-id='storyboard-creation-nav-done'] button, "
               + button("Publish", "Опублікувати", "Опубликовать"),
    "published": "text=/pin (has been )?published|опубліковано|опубликован/i",  # verify
}


class PinterestPublisher(BrowserPublisher):
    platform = "pinterest"
    login_url = URLS["login"]
    check_url = URLS["check"]
    cookie_urls = ("https://www.pinterest.com",)
    login_cookies = frozenset({"_auth"})

    async def is_logged_in(self, page) -> bool:
        if self._on_login_page(page.url):
            return False
        cookies = await page.context.cookies(list(self.cookie_urls))
        return any(c["name"] == "_auth" and c["value"] == "1" for c in cookies)  # _auth=0 when logged out

    async def _publish(self, page, video: Path, text: PlatformText, dry_run: bool) -> PublishResult:
        await self.open(page, URLS["create"])
        await self.upload_file(page, video, SELECTORS["file_input"])
        self.go("fill title")
        await self.fill(page, SELECTORS["title"], text.title)
        self.go("fill description")
        await self.fill(page, SELECTORS["description"], text.caption())
        board = page.locator(SELECTORS["board_dropdown"]).first
        if await board.count() and not (await board.inner_text()).strip():
            self.go("choose first board")
            await self.click(page, SELECTORS["board_dropdown"])
            await self.click(page, SELECTORS["board_first"])
        self.go("wait until video is uploaded")
        if not await self.wait_enabled(page, SELECTORS["publish"], self.upload_timeout(video)):
            return PublishResult("failed", error_code="upload", error="Publish button stayed disabled")
        if dry_run:
            return self.dry_run_result()
        self.go("click Publish")
        await self.click(page, SELECTORS["publish"])
        await self.confirm(page, SELECTORS["published"], self.upload_timeout(video))
        return PublishResult("published", url=None)
