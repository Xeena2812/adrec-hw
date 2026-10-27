"""Domain models shared by the API, the repositories and the event processing."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, PositiveInt, model_validator


class EvidenceType(StrEnum):
    """Which evidence the context gatherer collects for a condition.

    FRAME: trigger frame of the current event.
    CLIP: trigger frame + clip of the current event.
    HISTORY: trigger frames of all events in the history window.
    """

    FRAME = "frame"
    CLIP = "clip"
    HISTORY = "history"


class VerdictValue(StrEnum):
    YES = "yes"
    NO = "no"
    UNSURE = "unsure"


class Event(BaseModel):
    """A camera event. Paths are plain strings; the prototype has no media files."""

    id: str
    timestamp: datetime
    frame_path: str
    clip_path: str


class ConditionSpec(BaseModel):
    """Structured spec compiled once from a condition's free text."""

    evidence_type: EvidenceType
    history_window_seconds: PositiveInt | None = None

    @model_validator(mode="after")
    def _window_only_for_history(self) -> "ConditionSpec":
        has_window = self.history_window_seconds is not None
        if self.evidence_type is EvidenceType.HISTORY and not has_window:
            raise ValueError("HISTORY conditions need history_window_seconds")
        if self.evidence_type is not EvidenceType.HISTORY and has_window:
            raise ValueError("history_window_seconds is only allowed for HISTORY conditions")
        return self


class Condition(BaseModel):
    id: int
    text: str
    spec: ConditionSpec
    created_at: datetime


class Context(BaseModel):
    """Evidence handed to the VLM for one (event, condition) pair.

    `events` is ordered oldest first; the current event is always the last one.
    """

    events: list[Event] = Field(min_length=1)
    clip_path: str | None = None


class VLMResult(BaseModel):
    verdict: VerdictValue
    reasoning: str


class Verdict(BaseModel):
    """One VLM decision for one (event, condition) pair."""

    id: int
    created_at: datetime
    condition_id: int
    event_id: str  # the event that triggered the evaluation
    verdict: VerdictValue
    reasoning: str
    evidence_event_ids: list[str]  # every event whose frame the VLM saw


class Alert(BaseModel):
    id: int
    condition_id: int
    created_at: datetime
