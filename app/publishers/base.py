"""Common adapter interface. Every platform adapter follows the Publisher protocol."""
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Protocol

from app.texts import PlatformText

LogFn = Callable[[str], None]


@dataclass
class PublishResult:
    status: str  # published | dry_run | failed
    url: str | None = None
    error: str | None = None
    error_code: str | None = None
    screenshot: str | None = None


class NeedsUserAction(Exception):
    """Captcha, 2FA or login is required.

    code: "login" | "captcha" | "2fa".
    check: async callable that returns True once the user has resolved it
    (None means "wait until is_logged_in() is True").
    """

    def __init__(self, code: str, check: Callable[[], Awaitable[bool]] | None = None):
        super().__init__(code)
        self.code = code
        self.check = check


class Publisher(Protocol):
    platform: str
    uses_browser: bool
    login_url: str  # opened by the "Увійти" button
    check_url: str  # opened by "Перевірити вхід"

    def __init__(self, account, log: LogFn): ...

    async def is_logged_in(self, page) -> bool: ...

    async def publish(self, page, video: Path, text: PlatformText, dry_run: bool) -> PublishResult: ...
