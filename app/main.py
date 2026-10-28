"""Application wiring: database, services, the event queue and its processing loop.

Run with: uv run uvicorn app.main:app
The database comes from DATABASE_URL (default: local Postgres on port 5432).
"""

import asyncio
import logging
import os
import queue
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from functools import partial

from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api import Services, router
from app.context import ContextGatherer
from app.demo import router as demo_router
from app.domain import Event
from app.processing import process_event
from app.repositories.sql import (
    SqlAlertRepository,
    SqlConditionRepository,
    SqlEventRepository,
    SqlVerdictRepository,
    create_tables,
)
from app.vlm import MockVLM

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/postgres"
POLL_INTERVAL_SECONDS = 0.2


async def run_queue_loop(
    events: queue.Queue[Event],
    handle: Callable[[Event], object],
    poll_interval: float = POLL_INTERVAL_SECONDS,
) -> None:
    """Hand queued events to `handle` in arrival order until the task is cancelled.

    `handle` is synchronous and may be slow (a VLM call), so it runs in a
    worker thread to keep the event loop, and with it the API, responsive. One
    event at a time, so arrival order is kept. When the queue is empty the
    loop sleeps for `poll_interval`. A failing event is logged and skipped, so
    one bad event doesn't stop processing.
    """
    while True:
        try:
            event = events.get_nowait()
        except queue.Empty:
            await asyncio.sleep(poll_interval)
            continue
        try:
            await asyncio.to_thread(handle, event)
        except Exception:
            logger.exception("processing event %s failed", event.id)
        finally:
            events.task_done()  # lets callers tell when an event has been fully handled


def sql_services(database_url: str) -> Services:
    engine = create_engine(database_url)
    create_tables(engine)
    sessions = sessionmaker(engine)
    return Services(
        events=SqlEventRepository(sessions),
        conditions=SqlConditionRepository(sessions),
        verdicts=SqlVerdictRepository(sessions),
        alerts=SqlAlertRepository(sessions),
        vlm=MockVLM(),
    )


def create_app(make_services: Callable[[], Services] | None = None) -> FastAPI:
    """Build the app; services are created at startup (Postgres unless `make_services` is given)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if make_services is not None:
            services = make_services()
        else:
            services = sql_services(os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL))
        app.state.services = services

        handle = partial(
            process_event,
            events=services.events,
            conditions=services.conditions,
            gatherer=ContextGatherer(services.events),
            vlm=services.vlm,
            verdicts=services.verdicts,
            alerts=services.alerts,
        )
        loop_task = asyncio.create_task(run_queue_loop(services.event_queue, handle))
        yield
        loop_task.cancel()
        with suppress(asyncio.CancelledError):
            await loop_task

    app = FastAPI(title="CCTV event analytics", lifespan=lifespan)
    app.include_router(router)
    app.include_router(demo_router)
    return app


app = create_app()
