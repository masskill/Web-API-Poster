"""Application entry point: uvicorn app.main:app"""
import asyncio
import base64
import binascii
import logging
import os
import secrets
import threading
import webbrowser
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from app import automation, bot, browser, config, db, mixer, videogen, web
from app.scheduler import Scheduler
from app.worker import Worker, interrupt_stale_jobs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx logs every request URL at INFO, and Telegram URLs contain the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
if os.getenv("LOG_FILE"):  # set by run.ps1 -LogFile (autostart runs without a visible console)
    _file = logging.FileHandler(os.environ["LOG_FILE"], encoding="utf-8")
    _file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    for _name in ("", "uvicorn", "uvicorn.access"):  # uvicorn.error propagates to "uvicorn"
        logging.getLogger(_name).addHandler(_file)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    interrupt_stale_jobs()
    mixer.requeue_interrupted()
    mixer.worker.start()
    browser.runner.start()
    worker = Worker()
    browser.runner.submit(worker.run_forever())
    scheduler = Scheduler(worker.wake, preflight=lambda run_at: browser.runner.submit(automation.preflight(run_at)))
    scheduler.start()
    app.state.start_automation = lambda: scheduler.start_automation(
        login_check=lambda: browser.runner.submit(automation.check_all_logins()),
        cleanup=automation.cleanup,
        inbox=lambda: automation.scan_inbox(on_created=scheduler.schedule),
        video_poll=lambda: asyncio.run(videogen.poll()),
        update_check=automation.check_update_quietly,
        bot_poll=lambda: asyncio.run(bot.poll(on_created=scheduler.schedule)),
    )
    app.state.start_automation()
    app.state.worker, app.state.scheduler = worker, scheduler
    if os.getenv("OPEN_BROWSER") == "1":  # set by start.bat; the server is listening a moment after startup
        threading.Timer(2, webbrowser.open, ["http://127.0.0.1:8000"]).start()
    yield
    scheduler.shutdown()
    browser.runner.stop()


app = FastAPI(title="Web-API-Poster", lifespan=lifespan)
app.include_router(web.router)


def password_ok(request: Request) -> bool:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("basic "):
        return False
    try:
        _, _, password = base64.b64decode(auth[6:]).decode("utf-8").partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return False
    return secrets.compare_digest(password.encode(), config.APP_PASSWORD.encode())


@app.middleware("http")
async def security(request: Request, call_next):
    if config.APP_PASSWORD and not password_ok(request):
        return PlainTextResponse("password required", status_code=401,
                                 headers={"WWW-Authenticate": 'Basic realm="Web-API-Poster"'})
    # CSRF: another site open in the same browser must not be able to post forms here.
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        source = request.headers.get("origin") or request.headers.get("referer")
        if source and urlsplit(source).netloc != request.headers.get("host", ""):
            return PlainTextResponse("cross-site request blocked", status_code=403)
    return await call_next(request)
