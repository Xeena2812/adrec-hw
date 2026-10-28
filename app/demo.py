"""Demo scenarios: scripted event sequences with their expected outcome.

Each scenario posts one condition and a few events whose scene notes describe
what is visible; MockVLM derives its verdicts from them. `run_scenario` pushes them through the real
pipeline (event queue, processing loop, repositories), waits for the verdicts
and compares them with the expected outcome. The same scenarios back the
`/demo` endpoints and the end-to-end tests.
"""

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.api import Services, get_services
from app.conditions import ConditionService
from app.domain import Alert, Condition, Event, SceneNote, Verdict, VerdictValue

YES, NO, UNSURE = VerdictValue.YES, VerdictValue.NO, VerdictValue.UNSURE


class ScenarioName(StrEnum):
    SINGLE_FRAME = "single-frame"
    MULTI_EVENT = "multi-event"
    INSUFFICIENT = "insufficient"
    PACKAGE_LEFT = "package-left"
    NEGATIVE = "negative"
    DEDUP = "dedup"


@dataclass(frozen=True)
class Step:
    """One camera event: when it fires, what is visible in it, and what should result."""

    offset_seconds: int
    note: SceneNote
    expected_verdict: VerdictValue
    expected_alert: bool


@dataclass(frozen=True)
class Scenario:
    name: ScenarioName
    description: str
    condition_text: str
    steps: tuple[Step, ...]


PACKAGE_CONDITION = "Alert me if someone leaves a package at the door and walks away"

# Every condition is checked against every event, so the scenarios must not
# produce a "yes" for each other's conditions: observation keywords are
# unique to one scenario, and no person is visible in three consecutive
# events of more than one scenario (the loitering threshold).
SCENARIOS: dict[ScenarioName, Scenario] = {
    s.name: s
    for s in (
        Scenario(
            ScenarioName.SINGLE_FRAME,
            "Decidable from the trigger frame of a single event.",
            "Alert me if you see smoke",
            (Step(0, SceneNote(observations={"smoke": YES}), YES, True),),
        ),
        Scenario(
            ScenarioName.MULTI_EVENT,
            "The same person shows up in three events in a row. The verdict changes because the history grows: "
            "one or two sightings are not enough to tell, the third settles it.",
            "Alert me if someone loiters in front of the embassy",
            (
                Step(0, SceneNote(people=["p1"]), UNSURE, False),
                Step(10, SceneNote(people=["p1"]), UNSURE, False),
                Step(20, SceneNote(people=["p1"]), YES, True),
            ),
        ),
        Scenario(
            ScenarioName.INSUFFICIENT,
            "A package is visible, but nobody is in any frame of the history: the evidence doesn't show "
            "who left it, so the verdict is unsure and nothing alerts.",
            PACKAGE_CONDITION,
            (
                Step(0, SceneNote(objects=["package"]), UNSURE, False),
                Step(10, SceneNote(objects=["package"]), UNSURE, False),
            ),
        ),
        Scenario(
            ScenarioName.PACKAGE_LEFT,
            "Temporal order matters: with the person still next to the package nothing is wrong, "
            "and once a later event shows the package alone, the history shows the person walked away.",
            PACKAGE_CONDITION,
            (
                Step(0, SceneNote(people=["p2"], objects=["package"]), NO, False),
                Step(10, SceneNote(objects=["package"]), YES, True),
            ),
        ),
        Scenario(
            ScenarioName.NEGATIVE,
            "The event fires, but the condition is not met.",
            "Alert me if someone is fleeing",
            (Step(0, SceneNote(observations={"fleeing": NO}), NO, False),),
        ),
        Scenario(
            ScenarioName.DEDUP,
            "A persisting situation re-fires every few seconds: only the first event alerts, "
            "and a new alert follows once the situation has been quiet for more than 10 seconds.",
            "Alert me if you see a fire",
            (
                Step(0, SceneNote(observations={"fire": YES}), YES, True),
                Step(10, SceneNote(observations={"fire": YES}), YES, False),
                Step(30, SceneNote(observations={"fire": YES}), YES, True),
            ),
        ),
    )
}


class StepReport(BaseModel):
    event: Event
    verdict: Verdict
    alert: Alert | None
    expected_verdict: VerdictValue
    expected_alert: bool
    ok: bool


class ScenarioReport(BaseModel):
    name: ScenarioName
    description: str
    condition: Condition
    steps: list[StepReport]
    passed: bool


class ScenarioTimeout(Exception):
    """The events were not processed in time (the processing loop is not running or keeps failing)."""


def run_scenario(services: Services, scenario: Scenario, timeout: float = 5.0) -> ScenarioReport:
    """Run the scenario through the queue and processing loop and report what came out.

    Safe to repeat on a database with earlier runs: the condition is reused if
    its text exists, event ids are unique per run, and the events are placed
    after every stored event so earlier runs never fall into a history window.
    """
    condition = next((c for c in services.conditions.get_all() if c.text == scenario.condition_text), None)
    if condition is None:
        condition = ConditionService(services.conditions, services.vlm).create(scenario.condition_text)

    stored = services.events.get_all()
    now = datetime.now(timezone.utc)
    start = max(now, stored[-1].timestamp + timedelta(hours=1)) if stored else now
    run_id = uuid4().hex[:8]
    events = []
    for number, step in enumerate(scenario.steps, start=1):
        event_id = f"{scenario.name}-{run_id}-{number}"
        events.append(
            Event(
                id=event_id,
                timestamp=start + timedelta(seconds=step.offset_seconds),
                frame_path=f"frames/{event_id}.jpg",
                clip_path=f"clips/{event_id}.mjpeg",
                note=step.note,
            )
        )
    for event in events:
        services.events.create(event)
        services.event_queue.put(event)

    verdicts = _wait_for_verdicts(services, condition.id, {e.id for e in events}, timeout)
    alerts = {a.verdict_id: a for a in services.alerts.get_all()}
    steps = []
    for event, step in zip(events, scenario.steps, strict=True):
        verdict = verdicts[event.id]
        alert = alerts.get(verdict.id)
        ok = verdict.verdict is step.expected_verdict and (alert is not None) is step.expected_alert
        steps.append(
            StepReport(
                event=event,
                verdict=verdict,
                alert=alert,
                expected_verdict=step.expected_verdict,
                expected_alert=step.expected_alert,
                ok=ok,
            )
        )
    return ScenarioReport(
        name=scenario.name,
        description=scenario.description,
        condition=condition,
        steps=steps,
        passed=all(s.ok for s in steps),
    )


def _wait_for_verdicts(
    services: Services, condition_id: int, event_ids: set[str], timeout: float
) -> dict[str, Verdict]:
    """The condition's verdicts for `event_ids`, once every one of them exists."""
    deadline = time.monotonic() + timeout
    while True:
        found = {
            v.event_id: v
            for v in services.verdicts.get_all(condition_id=condition_id)
            if v.event_id in event_ids
        }
        # The alert is created right after its verdict, so wait until every queued event
        # has been fully handled, not just until the verdicts exist.
        if len(found) == len(event_ids) and services.event_queue.unfinished_tasks == 0:
            return found
        if time.monotonic() > deadline:
            raise ScenarioTimeout(f"{len(found)} of {len(event_ids)} events processed after {timeout} s")
        time.sleep(0.02)


router = APIRouter(prefix="/demo", tags=["demo"])


@router.post(
    "/{case}",
    description="Posts the case's condition and events through the normal pipeline and returns the outcome "
    "next to what was expected.\n\n"
    + "\n".join(f"- **{s.name}**: {s.description}" for s in SCENARIOS.values()),
)
def run_demo_case(case: ScenarioName, services: Services = Depends(get_services)) -> ScenarioReport:
    try:
        return run_scenario(services, SCENARIOS[case])
    except ScenarioTimeout as exc:
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, str(exc))
