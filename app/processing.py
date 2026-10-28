"""Event processing: evaluating every condition against one event."""

import logging
from datetime import timedelta

from app.context import ContextGatherer
from app.domain import Event, Verdict, VerdictValue
from app.repositories.base import AlertRepository, ConditionRepository, EventRepository, VerdictRepository
from app.vlm import VLM

logger = logging.getLogger(__name__)

# A "yes" within this long of the previous "yes" for the same condition (by
# event time, inclusive) is the same situation continuing: no new alert.
# Cameras re-fire about every 10 s, so a jittery gap just over this ends the chain.
ALERT_DEDUP_WINDOW = timedelta(seconds=10)


def process_event(
    event: Event,
    *,
    events: EventRepository,
    conditions: ConditionRepository,
    gatherer: ContextGatherer,
    vlm: VLM,
    verdicts: VerdictRepository,
    alerts: AlertRepository,
    dedup_window: timedelta = ALERT_DEDUP_WINDOW,
) -> list[Verdict]:
    """Evaluate every condition against the event; a "yes" raises an alert unless it continues a recent one."""
    stored = []
    for condition in conditions.get_all():
        context = gatherer.gather(event, condition.spec)
        result = vlm.evaluate(condition.text, context)
        verdict = verdicts.create(condition.id, event.id, result, [e.id for e in context.events])
        logger.info(
            "event %s, condition %d: %s (%s)", event.id, condition.id, verdict.verdict, verdict.reasoning
        )
        if verdict.verdict is VerdictValue.YES:
            if _continues_recent_yes(event, verdict, verdicts, events, dedup_window):
                logger.info("event %s, condition %d: same situation as before, no new alert", event.id, condition.id)
            else:
                alert = alerts.create(condition.id, verdict.id)
                # The alert service is external; logging stands in for sending it.
                logger.warning(
                    "ALERT %d: %r met at event %s (verdict %d)", alert.id, condition.text, event.id, verdict.id
                )
        stored.append(verdict)
    return stored


def _continues_recent_yes(
    event: Event,
    verdict: Verdict,
    verdicts: VerdictRepository,
    events: EventRepository,
    window: timedelta,
) -> bool:
    """Is there an earlier "yes" for the same condition whose event is within `window` of this one?"""
    earlier = [
        v
        for v in verdicts.get_all(condition_id=verdict.condition_id, verdict=VerdictValue.YES)
        if v.id != verdict.id
    ]
    if not earlier:
        return False
    previous_event = events.get(earlier[-1].event_id)
    return abs(event.timestamp - previous_event.timestamp) <= window
