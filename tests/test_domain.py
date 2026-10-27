from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.domain import ConditionSpec, Context, Event, EvidenceType


def test_history_spec_accepts_window():
    spec = ConditionSpec(evidence_type=EvidenceType.HISTORY, history_window_seconds=60)
    assert spec.history_window_seconds == 60


def test_history_spec_requires_window():
    with pytest.raises(ValidationError, match="need history_window_seconds"):
        ConditionSpec(evidence_type=EvidenceType.HISTORY)


@pytest.mark.parametrize("evidence_type", [EvidenceType.FRAME, EvidenceType.CLIP])
def test_non_history_spec_rejects_window(evidence_type):
    with pytest.raises(ValidationError, match="only allowed for HISTORY"):
        ConditionSpec(evidence_type=evidence_type, history_window_seconds=60)


@pytest.mark.parametrize("evidence_type", [EvidenceType.FRAME, EvidenceType.CLIP])
def test_non_history_spec_without_window(evidence_type):
    spec = ConditionSpec(evidence_type=evidence_type)
    assert spec.history_window_seconds is None


@pytest.mark.parametrize("window", [0, -10])
def test_history_window_must_be_positive(window):
    with pytest.raises(ValidationError):
        ConditionSpec(evidence_type=EvidenceType.HISTORY, history_window_seconds=window)


def test_context_requires_at_least_one_event():
    with pytest.raises(ValidationError):
        Context(events=[])


def test_context_with_current_event():
    event = Event(
        id="evt_0001",
        timestamp=datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc),
        frame_path="frames/evt_0001.jpg",
        clip_path="clips/evt_0001.mjpeg",
    )
    context = Context(events=[event])
    assert context.events[-1] == event
    assert context.clip_path is None
