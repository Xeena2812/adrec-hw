"""HTTP routes. The Swagger page at /docs is the "simple UI" for now."""

import queue
from dataclasses import dataclass, field

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.conditions import ConditionService
from app.domain import Alert, Condition, Event, Verdict, VerdictValue
from app.repositories.base import (
    AlertRepository,
    ConditionRepository,
    DuplicateEventError,
    EventRepository,
    VerdictRepository,
)
from app.vlm import VLM


@dataclass
class Services:
    """Everything the routes and the processing loop share; built once in main.py."""

    events: EventRepository
    conditions: ConditionRepository
    verdicts: VerdictRepository
    alerts: AlertRepository
    vlm: VLM
    # Thread-safe: sync routes run in FastAPI's thread pool, event processing in a worker thread.
    event_queue: queue.Queue[Event] = field(default_factory=queue.Queue)


def get_services(request: Request) -> Services:
    return request.app.state.services


class ConditionCreate(BaseModel):
    text: str = Field(min_length=1)


router = APIRouter()


@router.post("/conditions", status_code=status.HTTP_201_CREATED)
def create_condition(body: ConditionCreate, services: Services = Depends(get_services)) -> Condition:
    return ConditionService(services.conditions, services.vlm).create(body.text)


@router.get("/conditions")
def list_conditions(services: Services = Depends(get_services)) -> list[Condition]:
    return services.conditions.get_all()


@router.post("/events", status_code=status.HTTP_202_ACCEPTED)
def ingest_event(event: Event, services: Services = Depends(get_services)) -> Event:
    """Store the event and queue it; verdicts appear under /verdicts once it's processed."""
    try:
        services.events.create(event)
    except DuplicateEventError:
        raise HTTPException(status.HTTP_409_CONFLICT, f"event {event.id} already exists")
    services.event_queue.put(event)
    return event


@router.get("/verdicts")
def list_verdicts(
    condition_id: int | None = None,
    verdict: VerdictValue | None = None,
    services: Services = Depends(get_services),
) -> list[Verdict]:
    return services.verdicts.get_all(condition_id=condition_id, verdict=verdict)


@router.get("/alerts")
def list_alerts(services: Services = Depends(get_services)) -> list[Alert]:
    return services.alerts.get_all()
