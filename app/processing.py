"""Event processing: evaluating every condition against one event."""

import logging

from app.context import ContextGatherer
from app.domain import Event, Verdict, VerdictValue
from app.repositories.base import AlertRepository, ConditionRepository, VerdictRepository
from app.vlm import VLM

logger = logging.getLogger(__name__)


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
            alert = alerts.create(condition.id, verdict.id)
            # The alert service is external; logging stands in for sending it.
            logger.warning(
                "ALERT %d: %r met at event %s (verdict %d)", alert.id, condition.text, event.id, verdict.id
            )
        stored.append(verdict)
    return stored

