"""In-memory repositories for unit tests.

They follow the same contract as the SQL repositories, but don't check
foreign keys (e.g. that a verdict's condition exists).
"""

from datetime import datetime, timezone
from itertools import count

from app.domain import Alert, Condition, ConditionSpec, Event, Verdict, VerdictValue, VLMResult
from app.repositories.base import (
    AlertRepository,
    ConditionRepository,
    DuplicateEventError,
    EventRepository,
    VerdictRepository,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryEventRepository(EventRepository):
    def __init__(self) -> None:
        self._events: dict[str, Event] = {}

    def create(self, event: Event) -> Event:
        if event.id in self._events:
            raise DuplicateEventError(event.id)
        self._events[event.id] = event
        return event

    def get(self, event_id: str) -> Event | None:
        return self._events.get(event_id)

    def get_all(self) -> list[Event]:
        return sorted(self._events.values(), key=lambda e: e.timestamp)

    def get_between(self, start: datetime, end: datetime) -> list[Event]:
        return [e for e in self.get_all() if start <= e.timestamp <= end]


class InMemoryConditionRepository(ConditionRepository):
    def __init__(self) -> None:
        self._conditions: dict[int, Condition] = {}
        self._ids = count(1)

    def create(self, text: str, spec: ConditionSpec) -> Condition:
        condition = Condition(id=next(self._ids), text=text, spec=spec, created_at=_now())
        self._conditions[condition.id] = condition
        return condition

    def get(self, condition_id: int) -> Condition | None:
        return self._conditions.get(condition_id)

    def get_all(self) -> list[Condition]:
        return list(self._conditions.values())


class InMemoryVerdictRepository(VerdictRepository):
    def __init__(self) -> None:
        self._verdicts: dict[int, Verdict] = {}
        self._ids = count(1)

    def create(
        self,
        condition_id: int,
        event_id: str,
        result: VLMResult,
        evidence_event_ids: list[str],
    ) -> Verdict:
        verdict = Verdict(
            id=next(self._ids),
            created_at=_now(),
            condition_id=condition_id,
            event_id=event_id,
            verdict=result.verdict,
            reasoning=result.reasoning,
            evidence_event_ids=list(evidence_event_ids),
        )
        self._verdicts[verdict.id] = verdict
        return verdict

    def get(self, verdict_id: int) -> Verdict | None:
        return self._verdicts.get(verdict_id)

    def get_all(
        self,
        condition_id: int | None = None,
        verdict: VerdictValue | None = None,
    ) -> list[Verdict]:
        return [
            v
            for v in self._verdicts.values()
            if (condition_id is None or v.condition_id == condition_id)
            and (verdict is None or v.verdict == verdict)
        ]


class InMemoryAlertRepository(AlertRepository):
    def __init__(self) -> None:
        self._alerts: dict[int, Alert] = {}
        self._ids = count(1)

    def create(self, condition_id: int) -> Alert:
        alert = Alert(id=next(self._ids), condition_id=condition_id, created_at=_now())
        self._alerts[alert.id] = alert
        return alert

    def get(self, alert_id: int) -> Alert | None:
        return self._alerts.get(alert_id)

    def get_all(self) -> list[Alert]:
        return list(self._alerts.values())
