"""Repository interfaces for the four tables.

Every repository uses the same method names: `create`, `get` (by id) and
`get_all` (optionally filtered). There are no deletes; to start from scratch,
drop the database.
"""

from abc import ABC, abstractmethod
from datetime import datetime

from app.domain import Alert, Condition, ConditionSpec, Event, Verdict, VerdictValue, VLMResult


class DuplicateEventError(Exception):
    """Raised when an event with the same id is stored twice."""


class EventRepository(ABC):
    @abstractmethod
    def create(self, event: Event) -> Event:
        """Store an event. Raises DuplicateEventError if its id already exists."""

    @abstractmethod
    def get(self, event_id: str) -> Event | None: ...

    @abstractmethod
    def get_all(self) -> list[Event]:
        """All events, oldest first."""

    @abstractmethod
    def get_between(self, start: datetime, end: datetime) -> list[Event]:
        """Events with start <= timestamp <= end, oldest first."""


class ConditionRepository(ABC):
    @abstractmethod
    def create(self, text: str, spec: ConditionSpec) -> Condition:
        """Store a condition; the repository assigns id and created_at."""

    @abstractmethod
    def get(self, condition_id: int) -> Condition | None: ...

    @abstractmethod
    def get_all(self) -> list[Condition]:
        """All conditions, in creation order."""


class VerdictRepository(ABC):
    @abstractmethod
    def create(
        self,
        condition_id: int,
        event_id: str,
        result: VLMResult,
        evidence_event_ids: list[str],
    ) -> Verdict:
        """Store a verdict together with the events the VLM saw."""

    @abstractmethod
    def get(self, verdict_id: int) -> Verdict | None: ...

    @abstractmethod
    def get_all(
        self,
        condition_id: int | None = None,
        verdict: VerdictValue | None = None,
    ) -> list[Verdict]:
        """Verdicts in creation order, optionally filtered by condition and/or value."""


class AlertRepository(ABC):
    @abstractmethod
    def create(self, condition_id: int) -> Alert:
        """Store an alert; the repository assigns id and created_at."""

    @abstractmethod
    def get(self, alert_id: int) -> Alert | None: ...

    @abstractmethod
    def get_all(self) -> list[Alert]:
        """All alerts, in creation order."""
