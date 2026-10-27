from app.conditions import ConditionService
from app.domain import ConditionSpec, Context, EvidenceType, VLMResult
from app.repositories.memory import InMemoryConditionRepository
from app.vlm import VLM


class ClassifyingVLM(VLM):
    """Returns a fixed spec and records the texts it was asked to classify."""

    def __init__(self, spec: ConditionSpec) -> None:
        self.spec = spec
        self.classified: list[str] = []

    def classify_condition(self, condition_text: str) -> ConditionSpec:
        self.classified.append(condition_text)
        return self.spec

    def evaluate(self, condition_text: str, context: Context) -> VLMResult:
        raise AssertionError("evaluate must not be called when creating a condition")


def test_create_stores_condition_with_spec_from_vlm():
    spec = ConditionSpec(evidence_type=EvidenceType.HISTORY, history_window_seconds=90)
    vlm = ClassifyingVLM(spec)
    repo = InMemoryConditionRepository()

    condition = ConditionService(repo, vlm).create("alert me if someone loiters")

    assert vlm.classified == ["alert me if someone loiters"]
    assert condition.text == "alert me if someone loiters"
    assert condition.spec == spec
    assert repo.get(condition.id) == condition
