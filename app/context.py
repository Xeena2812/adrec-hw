"""Deterministic selection of the evidence the VLM sees for a condition."""

from datetime import timedelta

from app.domain import ConditionSpec, Context, Event, EvidenceType
from app.repositories.base import EventRepository


class ContextGatherer:
    def __init__(self, events: EventRepository) -> None:
        self._events = events

    def gather(self, event: Event, spec: ConditionSpec) -> Context:
        match spec.evidence_type:
            case EvidenceType.FRAME:
                return Context(events=[event])
            case EvidenceType.CLIP:
                return Context(events=[event], clip_path=event.clip_path)
            case EvidenceType.HISTORY:
                start = event.timestamp - timedelta(seconds=spec.history_window_seconds)
                # Reading only up to the current timestamp keeps later events out
                # of the history, even if they were stored before this one is processed.
                earlier = [e for e in self._events.get_between(start, event.timestamp) if e.id != event.id]
                return Context(events=[*earlier, event])
