from datetime import datetime, timezone

import pytest

from app.domain import Context, Event, EvidenceType, VerdictValue
from app.vlm import MockVLM


@pytest.mark.parametrize(
    ("text", "evidence_type", "window"),
    [
        ("alert me if you see smoke", EvidenceType.FRAME, None),
        ("someone flees the scene", EvidenceType.CLIP, None),
        ("alert me if someone loiters in front of the embassy", EvidenceType.HISTORY, 120),
        ("someone leaves a package at the door and walks away", EvidenceType.HISTORY, 60),
        # The package rule wins over "run", because multi-event rules come first.
        ("someone drops a parcel and runs off", EvidenceType.HISTORY, 60),
    ],
)
def test_classify_condition_keywords(text, evidence_type, window):
    spec = MockVLM().classify_condition(text)
    assert spec.evidence_type == evidence_type
    assert spec.history_window_seconds == window


def test_classify_condition_default_is_frame():
    spec = MockVLM().classify_condition("a red car stops at the gate")
    assert spec.evidence_type == EvidenceType.FRAME
    assert spec.history_window_seconds is None


def make_event(event_id: str, note: dict[str, VerdictValue]) -> Event:
    return Event(
        id=event_id,
        timestamp=datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc),
        frame_path="f.jpg",
        clip_path="c.mjpeg",
        note=note,
    )


@pytest.mark.parametrize("verdict", list(VerdictValue))
def test_evaluate_returns_the_noted_verdict(verdict):
    context = Context(events=[make_event("evt_0", {"smoke": verdict})])
    result = MockVLM().evaluate("Alert me if you see SMOKE", context)
    assert result.verdict is verdict
    assert "evt_0" in result.reasoning and "smoke" in result.reasoning


def test_evaluate_reads_the_note_of_the_current_event_only():
    earlier = make_event("evt_0", {"loiter": VerdictValue.NO})
    current = make_event("evt_1", {"loiter": VerdictValue.YES})
    result = MockVLM().evaluate("someone loiters", Context(events=[earlier, current]))
    assert result.verdict is VerdictValue.YES


def test_evaluate_without_a_matching_note_is_no():
    context = Context(events=[make_event("evt_0", {"smoke": VerdictValue.YES})])
    assert MockVLM().evaluate("someone loiters", context).verdict is VerdictValue.NO
    assert MockVLM().evaluate("someone loiters", Context(events=[make_event("evt_1", {})])).verdict is VerdictValue.NO
