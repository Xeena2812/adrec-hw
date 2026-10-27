import pytest

from app.domain import EvidenceType
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
