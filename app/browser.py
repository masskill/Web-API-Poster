"""Browser helpers: a dedicated thread for Playwright, persistent profiles, session files, debug artifacts."""
import asyncio
import json
import os
import random
import sys
import threading
from concurrent.futures import Future
from pathlib import Path

from app import config, settings_store
from app.db import get_session
from app.models import Account, utcnow


class BrowserThread:
    """Runs all browser work in one thread with its own event loop.

    On Windows the loop must be a ProactorEventLoop: Playwright starts a driver
    subprocess, which the selector loop used by uvicorn on Windows cannot do.
    """

    def __init__(self):
        self.loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread:
            return
        ready = threading.Event()

        def run():
            self.loop = asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            ready.set()
            self.loop.run_forever()

        self._thread = threading.Thread(target=run, name="browser", daemon=True)
        self._thread.start()
        ready.wait()

    def submit(self, coro) -> Future:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop(self) -> None:
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)


runner = BrowserThread()
# Only one browser session at a time (publishing, login and login checks share it).
BROWSER_LOCK = asyncio.Lock()


def profile_dir(account: Account) -> Path:
    return config.PROFILES_DIR / account.platform / account.slug


def session_file(account: Account) -> Path:
    return config.SESSIONS_DIR / account.platform / f"{account.slug}.json"


async def open_context(pw, account: Account, headless: bool, log=None):
    """Launch Edge (or fallback Chromium) with the account's own profile.

    If the profile is empty (e.g. a new server) but a session file exists,
    cookies and localStorage from that file are loaded, so the saved login keeps working.
    """
    profile = profile_dir(account)
    profile.mkdir(parents=True, exist_ok=True)
    fresh = not any(profile.iterdir())
    opts = {
        "headless": headless,
        "no_viewport": not headless,
        # Without these flags the browser reports itself as automated and Google
        # refuses the manual sign-in ("this browser may not be secure").
        "ignore_default_args": ["--enable-automation"],
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    channel = settings_store.get("browser_channel").strip()
    try:
        ctx = await pw.chromium.launch_persistent_context(str(profile), channel=channel or None, **opts)
    except Exception as e:
        if log:
            log(f"browser '{channel}' not available ({str(e).splitlines()[0]}), using Chromium")
        exe = os.getenv("BROWSER_EXECUTABLE") or None
        ctx = await pw.chromium.launch_persistent_context(str(profile), executable_path=exe, **opts)
    sf = session_file(account)
    if fresh and sf.exists():
        state = json.loads(sf.read_text(encoding="utf-8"))
        await ctx.add_cookies(state.get("cookies", []))
        storage = {o["origin"]: [[i["name"], i["value"]] for i in o.get("localStorage", [])]
                   for o in state.get("origins", [])}
        if storage:
            await ctx.add_init_script(script=RESTORE_STORAGE_JS % json.dumps(storage))
        if log:
            log("empty profile: cookies and localStorage loaded from session file")
    return ctx


# Puts saved localStorage items back (only those missing) when a page of that origin opens.
RESTORE_STORAGE_JS = """(saved => {
  const items = saved[location.origin];
  if (!items) return;
  try { for (const [k, v] of items) if (localStorage.getItem(k) === null) localStorage.setItem(k, v); } catch (e) {}
})(%s);"""


async def first_page(ctx):
    return ctx.pages[0] if ctx.pages else await ctx.new_page()


async def save_session(ctx, account: Account) -> Path:
    """Export cookies + localStorage ("keys" of the platform) and remember the login time."""
    path = session_file(account)
    path.parent.mkdir(parents=True, exist_ok=True)
    await ctx.storage_state(path=str(path))
    mark_logged_in(account.id, True)
    return path


def mark_logged_in(account_id: int, ok: bool) -> None:
    with get_session() as s:
        acc = s.get(Account, account_id)
        if acc:
            acc.logged_in_at = utcnow() if ok else None
            s.add(acc)
            s.commit()


async def human_pause(kind: str = "action") -> None:
    lo = settings_store.get_float(f"{kind}_pause_min")
    hi = settings_store.get_float(f"{kind}_pause_max")
    await asyncio.sleep(random.uniform(lo, max(lo, hi)))


async def wait_until(check, minutes: float, should_abort=None, interval: float = 3) -> bool:
    """Poll check() until it returns True, the time is up, or should_abort() says stop."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + minutes * 60
    while loop.time() < deadline:
        if should_abort and should_abort():
            return False
        try:
            if await check():
                return True
        except Exception:
            pass  # page is navigating; try again
        await asyncio.sleep(interval)
    return False


async def save_debug(job_id: int, log_lines: list[str], page=None) -> str:
    """Save steps.log, and screenshot.png + page.html if a page is open."""
    d = config.DEBUG_DIR / str(job_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "steps.log").write_text("\n".join(log_lines), encoding="utf-8")
    if page is not None and not page.is_closed():
        try:
            await page.screenshot(path=str(d / "screenshot.png"), full_page=True)
            (d / "page.html").write_text(await page.content(), encoding="utf-8")
        except Exception as e:
            with open(d / "steps.log", "a", encoding="utf-8") as f:
                f.write(f"\ncould not save page: {e}")
    return str(d)
