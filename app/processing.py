"""Event processing: evaluating one event, and the loop that drains the event queue."""

import asyncio
import logging
import queue
from collections.abc import Callable

from app.context import ContextGatherer
from app.domain import Event, Verdict, VerdictValue
from app.repositories.base import AlertRepository, ConditionRepository, VerdictRepository
from app.vlm import VLM

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 0.2


def process_event(
    event: Event,
    *,
    conditions: ConditionRepository,
    gatherer: ContextGatherer,
    vlm: VLM,
    verdicts: VerdictRepository,
    alerts: AlertRepository,
) -> list[Verdict]:
    """Evaluate every condition against the event; every "yes" raises an alert."""
    stored = []
    for condition in conditions.get_all():
        context = gatherer.gather(event, condition.spec)
        result = vlm.evaluate(condition.text, context)
        verdict = verdicts.create(condition.id, event.id, result, [e.id for e in context.events])
        logger.info(
            "event %s, condition %d: %s (%s)", event.id, condition.id, verdict.verdict, verdict.reasoning
        )
        if verdict.verdict is VerdictValue.YES:
            alert = alerts.create(condition.id)
            # The alert service is external; logging stands in for sending it.
            logger.warning("ALERT %d: %r met at event %s", alert.id, condition.text, event.id)
        stored.append(verdict)
    return stored


async def run_queue_loop(
    events: queue.Queue[Event],
    handle: Callable[[Event], object],
    poll_interval: float = POLL_INTERVAL_SECONDS,
) -> None:
    """Hand queued events to `handle` in arrival order until the task is cancelled.

    When the queue is empty the loop sleeps for `poll_interval`. A failing
    event is logged and skipped, so one bad event doesn't stop processing.
    """
    while True:
        try:
            event = events.get_nowait()
        except queue.Empty:
            await asyncio.sleep(poll_interval)
            continue
        try:
            handle(event)
        except Exception:
            logger.exception("processing event %s failed", event.id)
        # Let the API serve requests between events when the queue is busy.
        await asyncio.sleep(0)
