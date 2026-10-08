"""Application entry point: uvicorn app.main:app"""
import base64
import binascii
import logging
import secrets
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from app import browser, config, db, web
from app.scheduler import Scheduler
from app.worker import Worker, interrupt_stale_jobs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    interrupt_stale_jobs()
    browser.runner.start()
    worker = Worker()
    browser.runner.submit(worker.run_forever())
    scheduler = Scheduler(worker.wake)
    scheduler.start()
    app.state.worker, app.state.scheduler = worker, scheduler
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
