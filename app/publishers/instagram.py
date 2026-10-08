"""Instagram Reels via instagram.com in the browser (video posts are published as Reels)."""
from pathlib import Path

from app.publishers.base import BrowserPublisher, PublishResult, button
from app.texts import PlatformText

URLS = {
    "home": "https://www.instagram.com/",
    "login": "https://www.instagram.com/accounts/login/",
}

DIALOG = "div[role='dialog']"
SELECTORS = {
    # "Create" / "New post" item in the left menu; verify
    "create": "svg[aria-label='New post'], svg[aria-label='Create'], svg[aria-label='Створити'], "
              "svg[aria-label='Новий допис'], svg[aria-label='Создать'], svg[aria-label='Новая публикация']",
    # the "Post" item of the submenu that newer versions show after "Create"; verify
    "post_item": "svg[aria-label='Post'], svg[aria-label='Допис'], svg[aria-label='Публикация']",
    "file_input": f"{DIALOG} input[type='file'], form[enctype='multipart/form-data'] input[type='file']",
    "ok": f"{DIALOG} >> " + button("OK", "ОК", "Гаразд"),  # "Video posts are now shared as reels"
    "next": f"{DIALOG} >> " + button("Next", "Далі", "Далее"),
    "caption": f"{DIALOG} div[contenteditable='true'][role='textbox']",
    "share": f"{DIALOG} >> " + button("Share", "Поділитися", "Поделиться"),
    "shared": "text=/(reel|post) has been shared|ваш допис опубліковано|опубліковано|поширено|опубликовано/i",  # verify
}


class InstagramPublisher(BrowserPublisher):
    platform = "instagram"
    login_url = URLS["login"]
    check_url = URLS["home"]
    cookie_urls = ("https://www.instagram.com",)
    login_cookies = frozenset({"sessionid"})
    login_markers = ("/accounts/login",)

    async def _publish(self, page, video: Path, text: PlatformText, dry_run: bool) -> PublishResult:
        await self.open(page, URLS["home"])
        self.go("open Create dialog")
        await self.click(page, SELECTORS["create"])
        await self.click_if_visible(page, SELECTORS["post_item"], 3000)
        await self.upload_file(page, video, SELECTORS["file_input"])
        await self.click_if_visible(page, SELECTORS["ok"])
        for i in range(2):  # crop -> edit -> caption
            self.go(f"next ({i + 1}/2)")
            await self.click(page, SELECTORS["next"])
        self.go("fill caption")
        await self.fill(page, SELECTORS["caption"], text.caption())
        if dry_run:
            return self.dry_run_result()
        await self.submit(page, SELECTORS["share"])
        self.go("wait until the reel is shared")
        await self.confirm(page, SELECTORS["shared"], self.upload_timeout(video))
        return PublishResult("published", url=None)
