"""Contract tests for the repositories.

The `repos` fixture is parametrized over the backend and gives each test all
four repositories from that one backend. The SQL backend runs against the
Postgres database in TEST_DATABASE_URL and is skipped without it. Tests create
the conditions and events they reference, so they hold under real foreign keys.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from app.domain import ConditionSpec, Event, EvidenceType, VerdictValue, VLMResult
from app.repositories.base import (
    AlertRepository,
    ConditionRepository,
    DuplicateEventError,
    EventRepository,
    VerdictRepository,
)
from app.repositories.memory import (
    InMemoryAlertRepository,
    InMemoryConditionRepository,
    InMemoryEventRepository,
    InMemoryVerdictRepository,
)
from app.repositories.sql import (
    SqlAlertRepository,
    SqlConditionRepository,
    SqlEventRepository,
    SqlVerdictRepository,
)

T0 = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
FRAME_SPEC = ConditionSpec(evidence_type=EvidenceType.FRAME)
YES = VLMResult(verdict=VerdictValue.YES, reasoning="condition visible")
UNSURE = VLMResult(verdict=VerdictValue.UNSURE, reasoning="not enough evidence")


@dataclass
class Repos:
    events: EventRepository
    conditions: ConditionRepository
    verdicts: VerdictRepository
    alerts: AlertRepository


def memory_repos() -> Repos:
    return Repos(
        events=InMemoryEventRepository(),
        conditions=InMemoryConditionRepository(),
        verdicts=InMemoryVerdictRepository(),
        alerts=InMemoryAlertRepository(),
    )


def sql_repos(sessions) -> Repos:
    return Repos(
        events=SqlEventRepository(sessions),
        conditions=SqlConditionRepository(sessions),
        verdicts=SqlVerdictRepository(sessions),
        alerts=SqlAlertRepository(sessions),
    )


@pytest.fixture(params=["memory", "sql"])
def repos(request) -> Repos:
    if request.param == "memory":
        return memory_repos()
    return sql_repos(request.getfixturevalue("sql_sessions"))


def make_event(event_id: str, seconds: int) -> Event:
    return Event(
        id=event_id,
        timestamp=T0 + timedelta(seconds=seconds),
        frame_path=f"frames/{event_id}.jpg",
        clip_path=f"clips/{event_id}.mjpeg",
    )


def add_events(repos: Repos, *seconds: int) -> list[Event]:
    """Store events evt_0, evt_1, ... at the given offsets from T0."""
    return [repos.events.create(make_event(f"evt_{i}", s)) for i, s in enumerate(seconds)]


# --- events ---


def test_event_create_and_get(repos):
    event = make_event("evt_1", 0)
    repos.events.create(event)
    assert repos.events.get("evt_1") == event


def test_event_get_missing(repos):
    assert repos.events.get("nope") is None


def test_event_duplicate_id_rejected(repos):
    repos.events.create(make_event("evt_1", 0))
    with pytest.raises(DuplicateEventError):
        repos.events.create(make_event("evt_1", 10))


def test_event_get_all_oldest_first(repos):
    repos.events.create(make_event("evt_2", 10))
    repos.events.create(make_event("evt_1", 0))
    assert [e.id for e in repos.events.get_all()] == ["evt_1", "evt_2"]


def test_event_get_between_is_inclusive_and_ordered(repos):
    add_events(repos, 0, 10, 20, 30, 40)
    window = repos.events.get_between(T0 + timedelta(seconds=10), T0 + timedelta(seconds=30))
    assert [e.id for e in window] == ["evt_1", "evt_2", "evt_3"]


# --- conditions ---


def test_condition_create_assigns_id_and_created_at(repos):
    first = repos.conditions.create("alert me if you see smoke", FRAME_SPEC)
    second = repos.conditions.create("someone flees", ConditionSpec(evidence_type=EvidenceType.CLIP))
    assert first.id != second.id
    assert first.created_at is not None
    assert repos.conditions.get(first.id) == first


def test_condition_get_missing(repos):
    assert repos.conditions.get(999) is None


def test_condition_get_all_in_creation_order(repos):
    a = repos.conditions.create("a", FRAME_SPEC)
    b = repos.conditions.create("b", FRAME_SPEC)
    assert repos.conditions.get_all() == [a, b]


def test_condition_keeps_history_window(repos):
    spec = ConditionSpec(evidence_type=EvidenceType.HISTORY, history_window_seconds=120)
    condition = repos.conditions.create("loitering", spec)
    assert repos.conditions.get(condition.id).spec == spec


# --- verdicts ---


def test_verdict_create_and_get(repos):
    condition = repos.conditions.create("smoke", FRAME_SPEC)
    (event,) = add_events(repos, 0)
    verdict = repos.verdicts.create(condition.id, event.id, YES, [event.id])
    assert verdict.condition_id == condition.id
    assert verdict.event_id == event.id
    assert verdict.verdict == VerdictValue.YES
    assert verdict.reasoning == YES.reasoning
    assert verdict.evidence_event_ids == [event.id]
    assert repos.verdicts.get(verdict.id) == verdict


def test_verdict_evidence_oldest_first(repos):
    condition = repos.conditions.create("loitering", FRAME_SPEC)
    e0, e1, e2 = add_events(repos, 0, 10, 20)
    verdict = repos.verdicts.create(condition.id, e2.id, UNSURE, [e0.id, e1.id, e2.id])
    assert repos.verdicts.get(verdict.id).evidence_event_ids == [e0.id, e1.id, e2.id]


def test_verdict_get_missing(repos):
    assert repos.verdicts.get(999) is None


def test_verdict_get_all_filters(repos):
    c1 = repos.conditions.create("smoke", FRAME_SPEC)
    c2 = repos.conditions.create("fire", FRAME_SPEC)
    e0, e1 = add_events(repos, 0, 10)
    v1 = repos.verdicts.create(c1.id, e0.id, YES, [e0.id])
    v2 = repos.verdicts.create(c1.id, e1.id, UNSURE, [e1.id])
    v3 = repos.verdicts.create(c2.id, e1.id, UNSURE, [e1.id])

    assert repos.verdicts.get_all() == [v1, v2, v3]
    assert repos.verdicts.get_all(condition_id=c1.id) == [v1, v2]
    assert repos.verdicts.get_all(verdict=VerdictValue.UNSURE) == [v2, v3]
    assert repos.verdicts.get_all(condition_id=c2.id, verdict=VerdictValue.UNSURE) == [v3]
    assert repos.verdicts.get_all(condition_id=c2.id, verdict=VerdictValue.YES) == []


# --- alerts ---


def test_alert_create_and_get(repos):
    condition = repos.conditions.create("smoke", FRAME_SPEC)
    alert = repos.alerts.create(condition.id)
    assert alert.condition_id == condition.id
    assert repos.alerts.get(alert.id) == alert


def test_alert_get_missing(repos):
    assert repos.alerts.get(999) is None


def test_alert_get_all_in_creation_order(repos):
    c1 = repos.conditions.create("smoke", FRAME_SPEC)
    c2 = repos.conditions.create("fire", FRAME_SPEC)
    a = repos.alerts.create(c1.id)
    b = repos.alerts.create(c2.id)
    assert repos.alerts.get_all() == [a, b]


def test_sql_verdict_rejects_unknown_evidence(sql_sessions):
    repos = sql_repos(sql_sessions)
    condition = repos.conditions.create("smoke", FRAME_SPEC)
    (event,) = add_events(repos, 0)
    with pytest.raises(ValueError, match="evt_missing"):
        repos.verdicts.create(condition.id, event.id, YES, [event.id, "evt_missing"])
