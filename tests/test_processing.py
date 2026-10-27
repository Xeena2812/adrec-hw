import asyncio
import logging
import queue
from datetime import datetime, timedelta, timezone

from app.context import ContextGatherer
from app.domain import ConditionSpec, Context, Event, EvidenceType, VerdictValue, VLMResult
from app.processing import process_event, run_queue_loop
from app.repositories.memory import (
    InMemoryAlertRepository,
    InMemoryConditionRepository,
    InMemoryEventRepository,
    InMemoryVerdictRepository,
)
from app.vlm import VLM

T0 = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


def make_event(event_id: str, seconds: int) -> Event:
    return Event(
        id=event_id,
        timestamp=T0 + timedelta(seconds=seconds),
        frame_path=f"frames/{event_id}.jpg",
        clip_path=f"clips/{event_id}.mjpeg",
    )


class ScriptedVLM(VLM):
    """Returns a fixed verdict per condition text and records what it was shown."""

    def __init__(self, verdicts: dict[str, VerdictValue]) -> None:
        self.verdicts = verdicts
        self.seen: list[tuple[str, Context]] = []

    def classify_condition(self, condition_text: str) -> ConditionSpec:
        raise AssertionError("classify_condition must not be called while processing events")

    def evaluate(self, condition_text: str, context: Context) -> VLMResult:
        self.seen.append((condition_text, context))
        return VLMResult(verdict=self.verdicts[condition_text], reasoning=f"scripted for {condition_text}")


class Setup:
    def __init__(self, vlm_verdicts: dict[str, VerdictValue]) -> None:
        self.events = InMemoryEventRepository()
        self.conditions = InMemoryConditionRepository()
        self.verdicts = InMemoryVerdictRepository()
        self.alerts = InMemoryAlertRepository()
        self.vlm = ScriptedVLM(vlm_verdicts)

    def process(self, event: Event):
        return process_event(
            event,
            conditions=self.conditions,
            gatherer=ContextGatherer(self.events),
            vlm=self.vlm,
            verdicts=self.verdicts,
            alerts=self.alerts,
        )


FRAME = ConditionSpec(evidence_type=EvidenceType.FRAME)


def test_one_verdict_per_condition_and_alert_only_on_yes():
    setup = Setup({"smoke": VerdictValue.YES, "fire": VerdictValue.NO, "flood": VerdictValue.UNSURE})
    smoke = setup.conditions.create("smoke", FRAME)
    setup.conditions.create("fire", FRAME)
    setup.conditions.create("flood", FRAME)
    event = setup.events.create(make_event("evt_0", 0))

    stored = setup.process(event)

    assert [v.verdict for v in stored] == [VerdictValue.YES, VerdictValue.NO, VerdictValue.UNSURE]
    assert setup.verdicts.get_all() == stored
    assert all(v.event_id == "evt_0" for v in stored)
    assert [a.condition_id for a in setup.alerts.get_all()] == [smoke.id]


def test_verdict_records_history_evidence():
    setup = Setup({"loitering": VerdictValue.YES})
    setup.conditions.create(
        "loitering", ConditionSpec(evidence_type=EvidenceType.HISTORY, history_window_seconds=30)
    )
    for seconds in (0, 10, 20, 30, 40):
        setup.events.create(make_event(f"evt_{seconds}", seconds))

    (verdict,) = setup.process(setup.events.get("evt_30"))

    assert verdict.evidence_event_ids == ["evt_0", "evt_10", "evt_20", "evt_30"]
    (_, context) = setup.vlm.seen[0]
    assert [e.id for e in context.events] == verdict.evidence_event_ids


def test_no_conditions_means_no_verdicts():
    setup = Setup({})
    event = setup.events.create(make_event("evt_0", 0))
    assert setup.process(event) == []
    assert setup.alerts.get_all() == []


def test_alert_is_logged(caplog):
    setup = Setup({"smoke": VerdictValue.YES})
    setup.conditions.create("smoke", FRAME)
    event = setup.events.create(make_event("evt_0", 0))

    with caplog.at_level(logging.INFO, logger="app.processing"):
        setup.process(event)

    assert any("ALERT" in r.message and "'smoke'" in r.message for r in caplog.records)


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

    with caplog.at_level(logging.ERROR, logger="app.processing"):
        asyncio.run(_run_until(events, handle, lambda: calls))
    assert any("evt_bad" in r.message for r in caplog.records)
