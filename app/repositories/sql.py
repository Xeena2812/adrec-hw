"""Postgres repositories (SQLAlchemy 2.0 ORM).

The ORM row classes are internal to this module; repositories always return
domain models. Each repository method runs in its own transaction. Tables are
created at startup with create_tables(); there are no migrations.
"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, Engine, ForeignKey, Text, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from app.domain import Alert, Condition, ConditionSpec, Event, Verdict, VerdictValue, VLMResult
from app.repositories.base import (
    AlertRepository,
    ConditionRepository,
    DuplicateEventError,
    EventRepository,
    VerdictRepository,
)


class Base(DeclarativeBase):
    # Read server-generated values (ids, created_at) back on INSERT.
    __mapper_args__ = {"eager_defaults": True}


class EventRow(Base):
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    frame_path: Mapped[str] = mapped_column(Text)
    clip_path: Mapped[str] = mapped_column(Text)
    note: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)


class ConditionRow(Base):
    __tablename__ = "conditions"

    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    evidence_type: Mapped[str] = mapped_column(Text)
    history_window_seconds: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class VerdictRow(Base):
    __tablename__ = "verdicts"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    condition_id: Mapped[int] = mapped_column(ForeignKey("conditions.id"), index=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"))
    verdict: Mapped[str] = mapped_column(Text, index=True)
    reasoning: Mapped[str] = mapped_column(Text)
    evidence_events: Mapped[list[EventRow]] = relationship(
        secondary="verdict_evidence",
        order_by=(EventRow.timestamp, EventRow.id),
        lazy="selectin",
    )


class VerdictEvidenceRow(Base):
    __tablename__ = "verdict_evidence"

    verdict_id: Mapped[int] = mapped_column(ForeignKey("verdicts.id"), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"), primary_key=True)


class AlertRow(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    condition_id: Mapped[int] = mapped_column(ForeignKey("conditions.id"))
    verdict_id: Mapped[int] = mapped_column(ForeignKey("verdicts.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def create_tables(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def _to_event(row: EventRow) -> Event:
    return Event(
        id=row.id, timestamp=row.timestamp, frame_path=row.frame_path, clip_path=row.clip_path, note=row.note
    )


def _to_condition(row: ConditionRow) -> Condition:
    spec = ConditionSpec(evidence_type=row.evidence_type, history_window_seconds=row.history_window_seconds)
    return Condition(id=row.id, text=row.text, spec=spec, created_at=row.created_at)


def _to_verdict(row: VerdictRow) -> Verdict:
    return Verdict(
        id=row.id,
        created_at=row.created_at,
        condition_id=row.condition_id,
        event_id=row.event_id,
        verdict=row.verdict,
        reasoning=row.reasoning,
        evidence_event_ids=[e.id for e in row.evidence_events],
    )


def _to_alert(row: AlertRow) -> Alert:
    return Alert(id=row.id, condition_id=row.condition_id, verdict_id=row.verdict_id, created_at=row.created_at)


class SqlEventRepository(EventRepository):
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def create(self, event: Event) -> Event:
        try:
            with self._sessions.begin() as session:
                session.add(EventRow(**event.model_dump(mode="json")))
        except IntegrityError as exc:
            # Events have no foreign keys, so the only possible violation is the primary key.
            raise DuplicateEventError(event.id) from exc
        return event

    def get(self, event_id: str) -> Event | None:
        with self._sessions() as session:
            row = session.get(EventRow, event_id)
            return _to_event(row) if row else None

    def get_all(self) -> list[Event]:
        with self._sessions() as session:
            rows = session.scalars(select(EventRow).order_by(EventRow.timestamp))
            return [_to_event(r) for r in rows]

    def get_between(self, start: datetime, end: datetime) -> list[Event]:
        query = (
            select(EventRow)
            .where(EventRow.timestamp >= start, EventRow.timestamp <= end)
            .order_by(EventRow.timestamp)
        )
        with self._sessions() as session:
            return [_to_event(r) for r in session.scalars(query)]


class SqlConditionRepository(ConditionRepository):
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def create(self, text: str, spec: ConditionSpec) -> Condition:
        with self._sessions.begin() as session:
            row = ConditionRow(
                text=text,
                evidence_type=spec.evidence_type.value,
                history_window_seconds=spec.history_window_seconds,
            )
            session.add(row)
            session.flush()
            return _to_condition(row)

    def get(self, condition_id: int) -> Condition | None:
        with self._sessions() as session:
            row = session.get(ConditionRow, condition_id)
            return _to_condition(row) if row else None

    def get_all(self) -> list[Condition]:
        with self._sessions() as session:
            rows = session.scalars(select(ConditionRow).order_by(ConditionRow.id))
            return [_to_condition(r) for r in rows]


class SqlVerdictRepository(VerdictRepository):
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def create(
        self,
        condition_id: int,
        event_id: str,
        result: VLMResult,
        evidence_event_ids: list[str],
    ) -> Verdict:
        with self._sessions.begin() as session:
            evidence = session.scalars(select(EventRow).where(EventRow.id.in_(evidence_event_ids))).all()
            missing = set(evidence_event_ids) - {e.id for e in evidence}
            if missing:
                raise ValueError(f"unknown evidence events: {sorted(missing)}")
            row = VerdictRow(
                condition_id=condition_id,
                event_id=event_id,
                verdict=result.verdict.value,
                reasoning=result.reasoning,
                evidence_events=sorted(evidence, key=lambda e: (e.timestamp, e.id)),
            )
            session.add(row)
            session.flush()
            return _to_verdict(row)

    def get(self, verdict_id: int) -> Verdict | None:
        with self._sessions() as session:
            row = session.get(VerdictRow, verdict_id)
            return _to_verdict(row) if row else None

    def get_all(
        self,
        condition_id: int | None = None,
        verdict: VerdictValue | None = None,
    ) -> list[Verdict]:
        query = select(VerdictRow).order_by(VerdictRow.id)
        if condition_id is not None:
            query = query.where(VerdictRow.condition_id == condition_id)
        if verdict is not None:
            query = query.where(VerdictRow.verdict == verdict.value)
        with self._sessions() as session:
            return [_to_verdict(r) for r in session.scalars(query)]


class SqlAlertRepository(AlertRepository):
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def create(self, condition_id: int, verdict_id: int) -> Alert:
        with self._sessions.begin() as session:
            row = AlertRow(condition_id=condition_id, verdict_id=verdict_id)
            session.add(row)
            session.flush()
            return _to_alert(row)

    def get(self, alert_id: int) -> Alert | None:
        with self._sessions() as session:
            row = session.get(AlertRow, alert_id)
            return _to_alert(row) if row else None

    def get_all(self) -> list[Alert]:
        with self._sessions() as session:
            rows = session.scalars(select(AlertRow).order_by(AlertRow.id))
            return [_to_alert(r) for r in rows]
