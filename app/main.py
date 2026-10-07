"""Application entry point: uvicorn app.main:app"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import browser, db, web
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
