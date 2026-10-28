import asyncio
import logging
import queue
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.api import Services
from app.domain import Context, Event, VerdictValue, VLMResult
from app.main import create_app, run_queue_loop
from app.repositories.memory import (
    InMemoryAlertRepository,
    InMemoryConditionRepository,
    InMemoryEventRepository,
    InMemoryVerdictRepository,
)
from app.vlm import MockVLM

T0 = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


def make_event(event_id: str, seconds: int) -> Event:
    return Event(
        id=event_id,
        timestamp=T0 + timedelta(seconds=seconds),
        frame_path=f"frames/{event_id}.jpg",
        clip_path=f"clips/{event_id}.mjpeg",
    )


def event_json(event_id: str, seconds: int) -> dict:
    return make_event(event_id, seconds).model_dump(mode="json")


# --- queue loop ---


async def _run_until(events, handle, done, timeout: float = 2.0) -> None:
    """Run the queue loop until `done()` is true, then cancel it."""
    task = asyncio.create_task(run_queue_loop(events, handle, poll_interval=0.01))
    try:
        async with asyncio.timeout(timeout):
            while not done():
                await asyncio.sleep(0.01)
    finally:
        task.cancel()


def test_queue_loop_keeps_order_and_survives_a_failing_event():
    events: queue.Queue[Event] = queue.Queue()
    handled: list[str] = []

    def handle(event: Event) -> None:
        handled.append(event.id)
        if event.id == "evt_1":
            raise RuntimeError("boom")

    for i in range(3):
        events.put(make_event(f"evt_{i}", i * 10))

    asyncio.run(_run_until(events, handle, lambda: len(handled) == 3))
    assert handled == ["evt_0", "evt_1", "evt_2"]


def test_queue_loop_picks_up_events_added_while_idle():
    events: queue.Queue[Event] = queue.Queue()
    handled: list[str] = []

    async def scenario() -> None:
        loop_task = asyncio.create_task(_run_until(events, lambda e: handled.append(e.id), lambda: handled))
        await asyncio.sleep(0.05)  # the loop is polling an empty queue
        events.put(make_event("evt_late", 0))
        await loop_task

    asyncio.run(scenario())
    assert handled == ["evt_late"]


def test_queue_loop_logs_failures(caplog):
    events: queue.Queue[Event] = queue.Queue()
    events.put(make_event("evt_bad", 0))
    calls: list[str] = []

    def handle(event: Event) -> None:
        calls.append(event.id)
        raise RuntimeError("boom")

    with caplog.at_level(logging.ERROR, logger="app.main"):
        asyncio.run(_run_until(events, handle, lambda: calls))
    assert any("evt_bad" in r.message for r in caplog.records)


# --- API (in-memory services, real processing loop) ---


class ScriptedVLM(MockVLM):
    """Keyword classification from MockVLM, plus a fixed verdict per condition text."""

    def __init__(self, verdicts: dict[str, VerdictValue]) -> None:
        self.verdicts = verdicts

    def evaluate(self, condition_text: str, context: Context) -> VLMResult:
        return VLMResult(verdict=self.verdicts[condition_text], reasoning="scripted")


@pytest.fixture
def client():
    services = Services(
        events=InMemoryEventRepository(),
        conditions=InMemoryConditionRepository(),
        verdicts=InMemoryVerdictRepository(),
        alerts=InMemoryAlertRepository(),
        vlm=ScriptedVLM({"alert me if you see smoke": VerdictValue.YES, "someone flees": VerdictValue.NO}),
    )
    with TestClient(create_app(lambda: services)) as client:
        yield client


def wait_for_verdicts(client: TestClient, count: int, timeout: float = 2.0) -> list[dict]:
    deadline = time.monotonic() + timeout
    while True:
        verdicts = client.get("/verdicts").json()
        if len(verdicts) >= count or time.monotonic() > deadline:
            return verdicts
        time.sleep(0.02)


def test_create_condition_returns_spec_from_vlm(client):
    response = client.post("/conditions", json={"text": "alert me if someone loiters"})
    assert response.status_code == 201
    body = response.json()
    assert body["spec"] == {"evidence_type": "history", "history_window_seconds": 120}
    assert client.get("/conditions").json() == [body]


def test_create_condition_rejects_empty_text(client):
    assert client.post("/conditions", json={"text": ""}).status_code == 422


def test_post_event_is_accepted_and_duplicate_rejected(client):
    assert client.post("/events", json=event_json("evt_0", 0)).status_code == 202
    assert client.post("/events", json=event_json("evt_0", 10)).status_code == 409


def test_post_event_rejects_naive_timestamp(client):
    body = event_json("evt_0", 0) | {"timestamp": "2026-10-27T12:00:00"}
    assert client.post("/events", json=body).status_code == 422


def test_event_is_processed_into_verdicts_and_alerts(client):
    smoke = client.post("/conditions", json={"text": "alert me if you see smoke"}).json()
    flees = client.post("/conditions", json={"text": "someone flees"}).json()

    client.post("/events", json=event_json("evt_0", 0))
    verdicts = wait_for_verdicts(client, 2)

    assert {(v["condition_id"], v["verdict"]) for v in verdicts} == {(smoke["id"], "yes"), (flees["id"], "no")}
    assert all(v["evidence_event_ids"] == ["evt_0"] for v in verdicts)
    assert [v["condition_id"] for v in client.get("/verdicts", params={"verdict": "yes"}).json()] == [smoke["id"]]
    assert [v["verdict"] for v in client.get("/verdicts", params={"condition_id": flees["id"]}).json()] == ["no"]
    yes_verdict = next(v for v in verdicts if v["verdict"] == "yes")
    alerts = client.get("/alerts").json()
    assert [(a["condition_id"], a["verdict_id"]) for a in alerts] == [(smoke["id"], yes_verdict["id"])]


class BlockingVLM(ScriptedVLM):
    """Blocks inside evaluate until `release` is set, like a slow model call."""

    def __init__(self) -> None:
        super().__init__({"alert me if you see smoke": VerdictValue.YES})
        self.started = threading.Event()
        self.release = threading.Event()

    def evaluate(self, condition_text: str, context: Context) -> VLMResult:
        self.started.set()
        self.release.wait(timeout=5)
        return super().evaluate(condition_text, context)


def test_api_stays_responsive_while_the_vlm_is_evaluating():
    vlm = BlockingVLM()
    services = Services(
        events=InMemoryEventRepository(),
        conditions=InMemoryConditionRepository(),
        verdicts=InMemoryVerdictRepository(),
        alerts=InMemoryAlertRepository(),
        vlm=vlm,
    )
    with TestClient(create_app(lambda: services)) as client:
        try:
            client.post("/conditions", json={"text": "alert me if you see smoke"})
            client.post("/events", json=event_json("evt_0", 0))
            assert vlm.started.wait(timeout=2), "the VLM was never called"

            started = time.monotonic()
            assert client.get("/conditions").status_code == 200
            assert time.monotonic() - started < 0.5
            assert client.get("/verdicts").json() == []  # still evaluating
        finally:
            vlm.release.set()
        assert len(wait_for_verdicts(client, 1)) == 1
