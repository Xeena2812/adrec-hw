"""End-to-end tests: real app, real queue and processing loop, the real MockVLM.

Every test runs on the in-memory repositories and, when TEST_DATABASE_URL is
set, on Postgres too. Only the camera (the HTTP calls) and the pixels (the
scene notes on the events) are simulated.
"""

import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.api import Services
from app.demo import SCENARIOS, ScenarioName, ScenarioTimeout, run_scenario
from app.context import ContextGatherer
from app.domain import Context, Event, VerdictValue, VLMResult
from app.main import create_app
from app.repositories.memory import (
    InMemoryAlertRepository,
    InMemoryConditionRepository,
    InMemoryEventRepository,
    InMemoryVerdictRepository,
)
from app.repositories.sql import (
    SqlAlertRepository,
    SqlConditionRepository,
    SqlEventRepository,
    SqlVerdictRepository,
)
from app.vlm import VLM, MockVLM

T0 = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(params=["memory", "postgres"])
def services(request) -> Services:
    if request.param == "memory":
        return Services(
            events=InMemoryEventRepository(),
            conditions=InMemoryConditionRepository(),
            verdicts=InMemoryVerdictRepository(),
            alerts=InMemoryAlertRepository(),
            vlm=MockVLM(),
        )
    sessions = request.getfixturevalue("sql_sessions")  # skips without TEST_DATABASE_URL
    return Services(
        events=SqlEventRepository(sessions),
        conditions=SqlConditionRepository(sessions),
        verdicts=SqlVerdictRepository(sessions),
        alerts=SqlAlertRepository(sessions),
        vlm=MockVLM(),
    )


@pytest.fixture
def client(services):
    with TestClient(create_app(lambda: services)) as client:
        yield client


def post_event(client: TestClient, event_id: str, seconds: int, note: dict) -> None:
    body = {
        "id": event_id,
        "timestamp": (T0 + timedelta(seconds=seconds)).isoformat(),
        "frame_path": f"frames/{event_id}.jpg",
        "clip_path": f"clips/{event_id}.mjpeg",
        "note": note,
    }
    assert client.post("/events", json=body).status_code == 202


def wait_for_verdicts(client: TestClient, count: int, timeout: float = 5.0) -> list[dict]:
    deadline = time.monotonic() + timeout
    while True:
        verdicts = client.get("/verdicts").json()
        if len(verdicts) >= count or time.monotonic() > deadline:
            return verdicts
        time.sleep(0.02)


# --- the demo scenarios, through the /demo endpoints ---


@pytest.mark.parametrize("case", list(ScenarioName))
def test_demo_case_produces_the_expected_outcome(client, case):
    response = client.post(f"/demo/{case.value}")

    assert response.status_code == 200
    report = response.json()
    mismatches = [s for s in report["steps"] if not s["ok"]]
    assert report["passed"], mismatches
    assert len(report["steps"]) == len(SCENARIOS[case].steps)


def test_multi_event_verdicts_see_a_growing_history(client):
    report = client.post("/demo/multi-event").json()

    assert report["condition"]["spec"] == {"evidence_type": "history", "history_window_seconds": 120}
    event_ids = [s["event"]["id"] for s in report["steps"]]
    assert [s["verdict"]["evidence_event_ids"] for s in report["steps"]] == [
        event_ids[:1],
        event_ids[:2],
        event_ids[:3],
    ]
    assert [s["verdict"]["verdict"] for s in report["steps"]] == ["unsure", "unsure", "yes"]
    assert [s["alert"] is not None for s in report["steps"]] == [False, False, True]


def test_alert_links_to_its_verdict_and_events(client):
    report = client.post("/demo/single-frame").json()
    (step,) = report["steps"]

    (alert,) = client.get("/alerts").json()
    assert alert["verdict_id"] == step["verdict"]["id"] == step["alert"]["verdict_id"]
    assert alert["condition_id"] == report["condition"]["id"]
    assert step["verdict"]["event_id"] == step["event"]["id"]


def test_all_cases_in_a_row_leave_five_conditions_and_five_alerts(client):
    for case in ScenarioName:
        assert client.post(f"/demo/{case.value}").json()["passed"], case

    # insufficient and package-left share one condition text
    assert len(client.get("/conditions").json()) == len({s.condition_text for s in SCENARIOS.values()}) == 5
    # single-frame 1, multi-event 1, insufficient 0, package-left 1, negative 0, dedup 2
    assert len(client.get("/alerts").json()) == 5


def test_a_case_can_be_repeated_without_interfering_with_its_earlier_run(client):
    first = client.post("/demo/multi-event").json()
    second = client.post("/demo/multi-event").json()

    assert first["passed"] and second["passed"]
    assert second["condition"]["id"] == first["condition"]["id"]  # reused, not duplicated
    first_ids = {s["event"]["id"] for s in first["steps"]}
    second_evidence = {i for s in second["steps"] for i in s["verdict"]["evidence_event_ids"]}
    assert not first_ids & second_evidence
    assert len(client.get("/alerts").json()) == 2


def test_other_conditions_do_not_change_a_cases_outcome(client):
    # Every condition is checked against every event, so earlier cases' conditions see these events too.
    client.post("/demo/single-frame")
    client.post("/demo/negative")
    assert client.post("/demo/dedup").json()["passed"]


def test_demo_times_out_when_events_cannot_be_processed(services):
    class BrokenVLM(VLM):
        def classify_condition(self, condition_text):
            return MockVLM().classify_condition(condition_text)

        def evaluate(self, condition_text: str, context: Context) -> VLMResult:
            raise RuntimeError("model is down")

    services.vlm = BrokenVLM()
    with TestClient(create_app(lambda: services)):
        with pytest.raises(ScenarioTimeout):
            run_scenario(services, SCENARIOS[ScenarioName.SINGLE_FRAME], timeout=0.3)


def test_package_left_verdicts_follow_the_order_of_events(client):
    report = client.post("/demo/package-left").json()

    assert [s["verdict"]["verdict"] for s in report["steps"]] == ["no", "yes"]
    assert [s["alert"] is not None for s in report["steps"]] == [False, True]
    assert report["steps"][1]["verdict"]["evidence_event_ids"] == [s["event"]["id"] for s in report["steps"]]


def test_scenario_events_never_say_yes_to_another_scenarios_condition():
    vlm = MockVLM()
    for scenario in SCENARIOS.values():
        events = InMemoryEventRepository()
        for number, step in enumerate(scenario.steps):
            events.create(
                Event(
                    id=f"evt_{number}",
                    timestamp=T0 + timedelta(seconds=step.offset_seconds),
                    frame_path="f.jpg",
                    clip_path="c.mjpeg",
                    note=step.note,
                )
            )
        gatherer = ContextGatherer(events)
        for other in SCENARIOS.values():
            if other.condition_text == scenario.condition_text:
                continue
            spec = vlm.classify_condition(other.condition_text)
            for event in events.get_all():
                result = vlm.evaluate(other.condition_text, gatherer.gather(event, spec))
                assert result.verdict is not VerdictValue.YES, (scenario.name, other.name, event.id)


# --- the plain API, the way a camera backend and a user would use it ---


def test_conditions_and_events_through_the_public_api(client):
    smoke = client.post("/conditions", json={"text": "alert me if you see smoke"}).json()
    loiter = client.post("/conditions", json={"text": "alert me if someone loiters"}).json()
    assert smoke["spec"]["evidence_type"] == "frame"
    assert loiter["spec"] == {"evidence_type": "history", "history_window_seconds": 120}

    post_event(client, "evt_0", 0, {"people": ["p1"], "observations": {"smoke": "no"}})
    post_event(client, "evt_1", 10, {"people": ["p1"], "observations": {"smoke": "yes"}})
    post_event(client, "evt_2", 20, {"people": ["p1"]})
    verdicts = wait_for_verdicts(client, 6)

    by_event = {(v["event_id"], v["condition_id"]): v for v in verdicts}
    assert {k: v["verdict"] for k, v in by_event.items()} == {
        ("evt_0", smoke["id"]): "no",
        ("evt_0", loiter["id"]): "unsure",
        ("evt_1", smoke["id"]): "yes",
        ("evt_1", loiter["id"]): "unsure",
        ("evt_2", smoke["id"]): "no",
        ("evt_2", loiter["id"]): "yes",
    }
    assert by_event[("evt_1", smoke["id"])]["evidence_event_ids"] == ["evt_1"]
    assert by_event[("evt_2", loiter["id"])]["evidence_event_ids"] == ["evt_0", "evt_1", "evt_2"]

    alerts = client.get("/alerts").json()
    assert {a["verdict_id"] for a in alerts} == {
        by_event[("evt_1", smoke["id"])]["id"],
        by_event[("evt_2", loiter["id"])]["id"],
    }
    unsure = client.get("/verdicts", params={"verdict": "unsure"}).json()
    assert [v["condition_id"] for v in unsure] == [loiter["id"], loiter["id"]]


def test_event_without_a_note_is_a_no_for_every_condition(client):
    client.post("/conditions", json={"text": "alert me if you see smoke"})
    post_event(client, "evt_0", 0, {})

    (verdict,) = wait_for_verdicts(client, 1)
    assert verdict["verdict"] == VerdictValue.NO
    assert client.get("/alerts").json() == []
