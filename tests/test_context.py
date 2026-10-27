from datetime import datetime, timedelta, timezone

import pytest

from app.context import ContextGatherer
from app.domain import ConditionSpec, Event, EvidenceType
from app.repositories.memory import InMemoryEventRepository

T0 = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


def make_event(event_id: str, seconds: int) -> Event:
    return Event(
        id=event_id,
        timestamp=T0 + timedelta(seconds=seconds),
        frame_path=f"frames/{event_id}.jpg",
        clip_path=f"clips/{event_id}.mjpeg",
    )


def history(window: int) -> ConditionSpec:
    return ConditionSpec(evidence_type=EvidenceType.HISTORY, history_window_seconds=window)


@pytest.fixture
def repo():
    """Events at 0, 10, ..., 60 s, stored before any of them is processed."""
    repo = InMemoryEventRepository()
    for seconds in range(0, 70, 10):
        repo.create(make_event(f"evt_{seconds}", seconds))
    return repo


def test_frame_uses_only_current_event(repo):
    event = repo.get("evt_30")
    context = ContextGatherer(repo).gather(event, ConditionSpec(evidence_type=EvidenceType.FRAME))
    assert context.events == [event]
    assert context.clip_path is None


def test_clip_adds_current_clip(repo):
    event = repo.get("evt_30")
    context = ContextGatherer(repo).gather(event, ConditionSpec(evidence_type=EvidenceType.CLIP))
    assert context.events == [event]
    assert context.clip_path == "clips/evt_30.mjpeg"


def test_history_includes_window_oldest_first_and_no_later_events(repo):
    event = repo.get("evt_40")
    context = ContextGatherer(repo).gather(event, history(20))
    # 20 s before evt_40 is inclusive; evt_50 and evt_60 are in the future.
    assert [e.id for e in context.events] == ["evt_20", "evt_30", "evt_40"]
    assert context.clip_path is None


def test_history_without_earlier_events_is_just_current(repo):
    event = repo.get("evt_0")
    context = ContextGatherer(repo).gather(event, history(120))
    assert [e.id for e in context.events] == ["evt_0"]


def test_history_puts_current_event_last_even_if_not_stored():
    repo = InMemoryEventRepository()
    repo.create(make_event("evt_0", 0))
    event = make_event("evt_10", 10)
    context = ContextGatherer(repo).gather(event, history(60))
    assert [e.id for e in context.events] == ["evt_0", "evt_10"]
