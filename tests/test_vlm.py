from datetime import datetime, timedelta, timezone

import pytest

from app.domain import Context, Event, EvidenceType, SceneNote, VerdictValue
from app.vlm import LOITER_MAX_GAP, LOITER_MIN_FRAMES, MockVLM

YES, NO, UNSURE = VerdictValue.YES, VerdictValue.NO, VerdictValue.UNSURE
T0 = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
LOITER = "alert me if someone loiters in front of the embassy"
PACKAGE = "someone leaves a package at the door and walks away"


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


@pytest.mark.parametrize("text", ["someone hanging around", "a man lingers by the door", "LOITERING at night"])
def test_classify_condition_loitering_keywords(text):
    spec = MockVLM().classify_condition(text)
    assert (spec.evidence_type, spec.history_window_seconds) == (EvidenceType.HISTORY, 120)


@pytest.mark.parametrize("text", ["a parcel by the gate", "a bag is left behind", "a PACKAGE appears"])
def test_classify_condition_package_keywords(text):
    spec = MockVLM().classify_condition(text)
    assert (spec.evidence_type, spec.history_window_seconds) == (EvidenceType.HISTORY, 60)


@pytest.mark.parametrize("text", ["someone starts to run", "two people fight", "a person might fall"])
def test_classify_condition_clip_keywords(text):
    spec = MockVLM().classify_condition(text)
    assert (spec.evidence_type, spec.history_window_seconds) == (EvidenceType.CLIP, None)


def test_classify_condition_default_is_frame():
    spec = MockVLM().classify_condition("a red car stops at the gate")
    assert spec.evidence_type == EvidenceType.FRAME
    assert spec.history_window_seconds is None


def frame(
    seconds: int, people: tuple[str, ...] = (), objects: tuple[str, ...] = (), **observations: VerdictValue
) -> Event:
    """An event `seconds` after T0 whose frame shows the given people and objects."""
    return Event(
        id=f"evt_{seconds}",
        timestamp=T0 + timedelta(seconds=seconds),
        frame_path=f"frames/evt_{seconds}.jpg",
        clip_path=f"clips/evt_{seconds}.mjpeg",
        note=SceneNote(people=list(people), objects=list(objects), observations=observations),
    )


def evaluate(text: str, *events: Event):
    """Evaluate with the events as the context, oldest first, the current event last."""
    return MockVLM().evaluate(text, Context(events=list(events)))


# --- loitering ---


def test_loitering_is_no_when_nobody_is_in_the_current_frame():
    result = evaluate(LOITER, frame(0, ("p1",)), frame(10, ("p1",)), frame(20))
    assert result.verdict is NO
    assert result.reasoning.startswith("Mock:")


def test_loitering_is_unsure_for_a_first_sighting():
    result = evaluate(LOITER, frame(0, ("p1",)))
    assert result.verdict is UNSURE
    assert result.reasoning.startswith("Mock:")


def test_loitering_needs_the_minimum_consecutive_frames():
    assert LOITER_MIN_FRAMES == 3
    two = evaluate(LOITER, frame(0, ("p1",)), frame(10, ("p1",)))
    three = evaluate(LOITER, frame(0, ("p1",)), frame(10, ("p1",)), frame(20, ("p1",)))
    assert two.verdict is UNSURE
    assert three.verdict is YES
    assert "p1" in three.reasoning and "3 consecutive frames" in three.reasoning


def test_loitering_run_is_broken_by_a_frame_without_the_person():
    result = evaluate(LOITER, frame(0, ("p1",)), frame(10), frame(20, ("p1",)), frame(30, ("p1",)))
    assert result.verdict is UNSURE


def test_loitering_run_is_broken_by_a_frame_with_someone_else():
    result = evaluate(LOITER, frame(0, ("p1",)), frame(10, ("p2",)), frame(20, ("p1",)), frame(30, ("p1",)))
    assert result.verdict is UNSURE


def test_loitering_counts_each_person_separately():
    different = evaluate(LOITER, frame(0, ("p1",)), frame(10, ("p2",)), frame(20, ("p3",)))
    assert different.verdict is UNSURE
    # p2 is in all three frames, even though p1 and p3 come and go.
    together = evaluate(LOITER, frame(0, ("p1", "p2")), frame(10, ("p2",)), frame(20, ("p2", "p3")))
    assert together.verdict is YES
    assert "p2" in together.reasoning


def test_loitering_only_counts_people_in_the_current_frame():
    result = evaluate(LOITER, frame(0, ("p1",)), frame(10, ("p1",)), frame(20, ("p1",)), frame(30, ("p2",)))
    assert result.verdict is UNSURE


def test_loitering_run_is_broken_by_a_gap_over_the_limit():
    assert LOITER_MAX_GAP == timedelta(seconds=20)
    # 21 s between the first two frames: only the last two sightings form a run.
    result = evaluate(LOITER, frame(0, ("p1",)), frame(21, ("p1",)), frame(31, ("p1",)))
    assert result.verdict is UNSURE
    # The same gap right before the current frame leaves the current frame alone.
    result = evaluate(LOITER, frame(0, ("p1",)), frame(10, ("p1",)), frame(31, ("p1",)))
    assert result.verdict is UNSURE
    assert "1 consecutive frame(s)" in result.reasoning


def test_loitering_run_survives_a_gap_at_the_limit():
    result = evaluate(LOITER, frame(0, ("p1",)), frame(20, ("p1",)), frame(40, ("p1",)))
    assert result.verdict is YES


def test_loitering_gap_is_measured_between_adjacent_frames_not_across_the_run():
    # 60 s from first to last, but never more than 20 s between neighbours.
    events = [frame(20 * i, ("p1",)) for i in range(4)]
    assert evaluate(LOITER, *events).verdict is YES


def test_loitering_run_may_be_longer_than_the_minimum():
    events = [frame(10 * i, ("p1",)) for i in range(5)]
    assert evaluate(LOITER, *events).verdict is YES


# --- package left behind ---


def test_package_is_no_without_a_package_in_the_current_frame():
    assert evaluate(PACKAGE, frame(0, ("p1",)), frame(10, objects=("bag",))).verdict is NO
    # The package was there earlier but is gone now.
    assert evaluate(PACKAGE, frame(0, ("p1",), ("package",)), frame(10)).verdict is NO


def test_package_is_yes_when_the_person_seen_with_it_has_gone():
    result = evaluate(PACKAGE, frame(0, ("p1",), ("package",)), frame(10, objects=("package",)))
    assert result.verdict is YES
    assert result.reasoning.startswith("Mock:") and "p1" in result.reasoning


def test_package_is_yes_when_someone_else_is_in_the_current_frame():
    result = evaluate(PACKAGE, frame(0, ("p1",), ("package",)), frame(10, ("p2",), ("package",)))
    assert result.verdict is YES


def test_package_is_yes_even_with_an_empty_frame_in_between():
    history = [frame(0, ("p1",), ("package",)), frame(10, objects=("package",)), frame(20, objects=("package",))]
    assert evaluate(PACKAGE, *history).verdict is YES


def test_package_is_no_while_the_person_seen_with_it_is_still_there():
    result = evaluate(PACKAGE, frame(0, ("p1",), ("package",)), frame(10, ("p1",), ("package",)))
    assert result.verdict is NO
    assert evaluate(PACKAGE, frame(0, ("p1",), ("package",))).verdict is NO


def test_package_is_unsure_when_no_frame_shows_who_left_it():
    alone = evaluate(PACKAGE, frame(0, objects=("package",)))
    assert alone.verdict is UNSURE
    assert alone.reasoning.startswith("Mock:") and "frames/evt_0.jpg" in alone.reasoning
    assert evaluate(PACKAGE, frame(0, objects=("package",)), frame(10, objects=("package",))).verdict is UNSURE


def test_package_person_without_the_package_does_not_count():
    # p1 walked past before the package appeared; nothing links them to it.
    result = evaluate(PACKAGE, frame(0, ("p1",)), frame(10, objects=("package",)))
    assert result.verdict is UNSURE


# --- observations (clip and frame conditions) ---


@pytest.mark.parametrize("verdict", list(VerdictValue))
def test_observation_returns_the_observed_verdict(verdict):
    result = evaluate("Alert me if you see SMOKE", frame(0, smoke=verdict))
    assert result.verdict is verdict
    assert result.reasoning.startswith("Mock:") and "evt_0" in result.reasoning and "smoke" in result.reasoning


def test_observation_reads_the_current_event_only():
    assert evaluate("someone is fleeing", frame(0, fleeing=NO), frame(10, fleeing=YES)).verdict is YES
    assert evaluate("someone is fleeing", frame(0, fleeing=YES), frame(10)).verdict is NO


def test_observation_first_key_found_in_the_condition_wins():
    assert evaluate("smoke and fire", frame(0, fire=YES, smoke=NO)).verdict is YES  # fire comes first
    assert evaluate("only smoke", frame(0, fire=YES, smoke=NO)).verdict is NO


def test_observation_without_a_matching_key_is_no():
    assert evaluate("someone is fleeing", frame(0, smoke=YES)).verdict is NO
    assert evaluate("someone is fleeing", frame(0)).verdict is NO


def test_people_and_objects_do_not_decide_observation_conditions():
    assert evaluate("alert me if you see smoke", frame(0, ("p1",), ("package",))).verdict is NO


# --- one rule table ---


def test_classification_and_evaluation_use_the_same_rule():
    vlm = MockVLM()
    # Two sightings: the loitering rule says unsure, an observation rule would say no.
    context = Context(events=[frame(0, ("p1",)), frame(10, ("p1",))])
    assert vlm.classify_condition(LOITER).evidence_type is EvidenceType.HISTORY
    assert vlm.evaluate(LOITER, context).verdict is UNSURE

    # "runs" also matches the clip rule, but the package rule comes first for both.
    text = "someone drops a package and runs off"
    assert vlm.classify_condition(text).evidence_type is EvidenceType.HISTORY
    package_context = Context(events=[frame(0, ("p1",), ("package",)), frame(10, objects=("package",))])
    assert vlm.evaluate(text, package_context).verdict is YES


def test_unmatched_condition_is_a_frame_condition_read_from_observations():
    vlm = MockVLM()
    text = "a red car stops at the gate"
    assert vlm.classify_condition(text).evidence_type is EvidenceType.FRAME
    assert vlm.evaluate(text, Context(events=[frame(0, car=YES)])).verdict is YES
