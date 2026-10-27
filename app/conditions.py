"""Creating user conditions."""

from app.domain import Condition
from app.repositories.base import ConditionRepository
from app.vlm import VLM


class ConditionService:
    def __init__(self, conditions: ConditionRepository, vlm: VLM) -> None:
        self._conditions = conditions
        self._vlm = vlm

    def create(self, text: str) -> Condition:
        """Ask the VLM once which evidence the condition needs, then store it."""
        return self._conditions.create(text, self._vlm.classify_condition(text))
