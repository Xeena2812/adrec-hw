"""The vision-language model boundary."""

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from app.domain import ConditionSpec, Context, Event, EvidenceType, VerdictValue, VLMResult


class VLM(ABC):
    @abstractmethod
    def classify_condition(self, condition_text: str) -> ConditionSpec:
        """Decide which evidence a condition needs. Called once, when the condition is created."""

    @abstractmethod
    def evaluate(self, condition_text: str, context: Context) -> VLMResult:
        """Judge whether the condition holds, given only the evidence in context.

        The VLM never asks for more evidence; it returns yes, no or unsure
        plus its reasoning.
        """


# A person must be seen in this many consecutive frames, ending at the current
# one, before the mock calls it loitering.
LOITER_MIN_FRAMES = 3

# Two sightings of a person further apart than this are not one continuous
# stay. Cameras re-fire about every 10 s while a situation persists, so 20 s
# tolerates one missed re-fire; a gap of exactly this long still counts.
LOITER_MAX_GAP = timedelta(seconds=20)

Evaluator = Callable[[str, Context], VLMResult]


def _result(verdict: VerdictValue, reasoning: str) -> VLMResult:
    return VLMResult(verdict=verdict, reasoning=f"Mock: {reasoning}")


def _evaluate_loitering(condition_text: str, context: Context) -> VLMResult:
    """Loitering means the same person in the current frame and the frames just before it.

    no: nobody in the current frame. yes: someone's run of consecutive frames
    ending at the current one reaches LOITER_MIN_FRAMES. unsure: someone is
    there, but not (yet) long enough to tell. A frame without the person, or
    a gap of more than LOITER_MAX_GAP between two adjacent frames, breaks
    their run.
    """
    *earlier, current = context.events
    if not current.note.people:
        return _result(VerdictValue.NO, f"nobody visible in {current.frame_path}")
    runs = {person: _run_length(person, earlier, current) for person in current.note.people}
    person, run = max(runs.items(), key=lambda item: item[1])
    if run >= LOITER_MIN_FRAMES:
        return _result(VerdictValue.YES, f"{person} visible in {run} consecutive frames (>= {LOITER_MIN_FRAMES})")
    return _result(
        VerdictValue.UNSURE,
        f"{person} visible in {run} consecutive frame(s), fewer than the {LOITER_MIN_FRAMES} needed to tell",
    )


def _run_length(person: str, earlier: list[Event], current: Event) -> int:
    """Consecutive frames ending at the current one that show the person (who is in the current frame).

    Walking back from the current frame, the run ends at the first frame that
    lacks the person or lies more than LOITER_MAX_GAP before the frame after it.
    """
    run = 1
    later = current
    for event in reversed(earlier):
        if person not in event.note.people or later.timestamp - event.timestamp > LOITER_MAX_GAP:
            break
        run += 1
        later = event
    return run


def _evaluate_package_left(condition_text: str, context: Context) -> VLMResult:
    """A package is "left" when someone was seen with it earlier and is gone from the current frame.

    no: no package in the current frame, or the person seen with it is still
    there (they haven't walked away). yes: package in the current frame and an
    earlier frame shows it with a person who is not in the current frame.
    unsure: the package is visible but no frame shows anyone with it, so
    nothing says who left it.
    """
    *earlier, current = context.events
    if "package" not in current.note.objects:
        return _result(VerdictValue.NO, f"no package visible in {current.frame_path}")
    with_package = [(e, p) for e in earlier if "package" in e.note.objects for p in e.note.people]
    for event, person in with_package:
        if person not in current.note.people:
            return _result(
                VerdictValue.YES,
                f"{person} was with the package in {event.frame_path} and is gone from {current.frame_path}",
            )
    if with_package or current.note.people:
        return _result(VerdictValue.NO, "the person seen with the package is still there, nobody walked away")
    return _result(
        VerdictValue.UNSURE,
        f"package visible in {current.frame_path} but no frame shows who left it",
    )


def _evaluate_observation(condition_text: str, context: Context) -> VLMResult:
    """Read the current event's observations: the first key found in the condition text decides.

    No key found means the scripted scene doesn't contain the condition: no.
    """
    current = context.events[-1]
    lowered = condition_text.lower()
    for keyword, verdict in current.note.observations.items():
        if keyword.lower() in lowered:
            return _result(verdict, f"{current.id} observes {keyword!r}: {verdict.value}")
    return _result(VerdictValue.NO, f"no observation of {current.id} matches the condition")


@dataclass(frozen=True)
class Rule:
    """One kind of condition: the keywords that select it, the evidence it needs and how it is judged."""

    keywords: tuple[str, ...]
    spec: ConditionSpec
    evaluate: Evaluator


# The first rule with a keyword found in the condition text wins, so the more
# specific multi-event rules come first. Classification and evaluation both
# read this table, so they can't disagree.
_RULES: tuple[Rule, ...] = (
    Rule(
        ("loiter", "hanging around", "lingers"),
        ConditionSpec(evidence_type=EvidenceType.HISTORY, history_window_seconds=120),
        _evaluate_loitering,
    ),
    Rule(
        ("package", "parcel", "left behind"),
        ConditionSpec(evidence_type=EvidenceType.HISTORY, history_window_seconds=60),
        _evaluate_package_left,
    ),
    Rule(
        ("flee", "run", "fight", "fall"),
        ConditionSpec(evidence_type=EvidenceType.CLIP),
        _evaluate_observation,
    ),
)
_DEFAULT_RULE = Rule((), ConditionSpec(evidence_type=EvidenceType.FRAME), _evaluate_observation)


def _rule_for(condition_text: str) -> Rule:
    lowered = condition_text.lower()
    for rule in _RULES:
        if any(keyword in lowered for keyword in rule.keywords):
            return rule
    return _DEFAULT_RULE


class MockVLM(VLM):
    """Stand-in for the real ~27B Qwen VLM.

    In production the real VLM receives all the context it needs (the event
    frame, its clip, or the frames of earlier events), selected by
    deterministic code from the condition's spec, and only returns a final
    verdict. Swapping in the real model only replaces this class.

    The mock never looks at pixels. It reads the scene notes of all events in
    the context (who and what is visible, plus direct observations) and
    decides with the rule that matches the condition text, so what the
    context gatherer collected changes the verdict.
    """

    def classify_condition(self, condition_text: str) -> ConditionSpec:
        """Pick the evidence type from keywords; anything unrecognised gets the trigger frame."""
        return _rule_for(condition_text).spec

    def evaluate(self, condition_text: str, context: Context) -> VLMResult:
        """Judge the condition with the rule that classified it."""
        return _rule_for(condition_text).evaluate(condition_text, context)
