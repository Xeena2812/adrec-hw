"""The vision-language model boundary."""

from abc import ABC, abstractmethod

from app.domain import ConditionSpec, Context, EvidenceType, VerdictValue, VLMResult


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


# Keyword rules for MockVLM.classify_condition: the first rule with a keyword
# found in the text wins, so the more specific multi-event rules come first.
_CLASSIFY_RULES: list[tuple[tuple[str, ...], ConditionSpec]] = [
    (
        ("loiter", "hanging around", "lingers"),
        ConditionSpec(evidence_type=EvidenceType.HISTORY, history_window_seconds=120),
    ),
    (
        ("package", "parcel", "left behind"),
        ConditionSpec(evidence_type=EvidenceType.HISTORY, history_window_seconds=60),
    ),
    (
        ("flee", "run", "fight", "fall"),
        ConditionSpec(evidence_type=EvidenceType.CLIP),
    ),
]
_DEFAULT_SPEC = ConditionSpec(evidence_type=EvidenceType.FRAME)


class MockVLM(VLM):
    """Stand-in for the real ~27B Qwen VLM.

    In production the real VLM receives all the context it needs (the event
    frame, its clip, or the frames of earlier events), selected by
    deterministic code from the condition's spec, and only returns a final
    verdict. Swapping in the real model only replaces this class.

    The mock never looks at pixels. It reads the ground-truth note of the
    current event (the last one in the context), which maps a keyword to the
    verdict the real model would reach after seeing the whole context.
    """

    def classify_condition(self, condition_text: str) -> ConditionSpec:
        """Pick the evidence type from keywords; anything unrecognised gets the trigger frame."""
        lowered = condition_text.lower()
        for keywords, spec in _CLASSIFY_RULES:
            if any(keyword in lowered for keyword in keywords):
                return spec
        return _DEFAULT_SPEC

    def evaluate(self, condition_text: str, context: Context) -> VLMResult:
        """Take the verdict from the first note keyword found in the condition text.

        A condition that no note keyword matches is a plain "no": the scripted
        scene doesn't contain it.
        """
        event = context.events[-1]
        lowered = condition_text.lower()
        for keyword, verdict in event.note.items():
            if keyword.lower() in lowered:
                return VLMResult(
                    verdict=verdict,
                    reasoning=f"Mock: ground-truth note of {event.id} says {keyword!r} is {verdict.value}",
                )
        return VLMResult(
            verdict=VerdictValue.NO,
            reasoning=f"Mock: no ground-truth note of {event.id} matches the condition",
        )
