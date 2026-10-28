# CCTV event analytics backend

A prototype backend that checks free-text user alert conditions ("alert me if someone loiters in front of the embassy") against CCTV camera events. A camera event carries a trigger frame and a short clip, and re-fires about every 10 s while a situation persists. For each event, every condition is judged by a vision-language model (VLM) as `yes`, `no` or `unsure`, with reasoning. A `yes` raises an alert linked to its verdict and evidence events. The ~27B Qwen VLM is mocked: it reads a hand-written scene note per event instead of pixels. FastAPI + Postgres, started with Docker Compose.

## Quick start

```sh
docker compose up --build
```

The API listens on http://localhost:8000, and the Swagger UI at http://localhost:8000/docs is the "simple UI". Alerts are also logged by the `api` container (`ALERT ...` lines).

`docker compose down` deletes all data (Postgres has no volume).

## Demo

`POST /demo/{case}` creates the case's condition, posts its events through the normal pipeline (event queue, processing loop, Postgres), waits for the verdicts and returns them next to the expected outcome. Cases can be run in any order and repeated; each run's events are placed after all stored events, so earlier runs never fall into a history window (timestamps therefore drift into the future on repeated runs).

| Case | Condition | Events (time, what the scene note shows) | Verdicts | Covers |
| --- | --- | --- | --- | --- |
| `single-frame` | Alert me if you see smoke | 0 s: smoke | `yes`, alert | 1. decidable from a single frame |
| `multi-event` | Alert me if someone loiters in front of the embassy | 0, 10, 20 s: the same person `p1` | `unsure`, `unsure`, `yes` (alert on the third) | 2. needs several events: the history grows with each event |
| `package-left` | Alert me if someone leaves a package at the door and walks away | 0 s: person `p2` with a package; 10 s: package alone | `no`, `yes` (alert) | 2. order matters: the earlier event shows who walked away |
| `insufficient` | same package condition | 0, 10 s: package visible, nobody in any frame | `unsure`, `unsure`; no alert | 3. not enough information to decide |
| `negative` | Alert me if someone is fleeing | 0 s: the event fires, the scene shows no fleeing | `no`, no alert | 4. event fires, condition not met |
| `dedup` | Alert me if you see a fire | 0, 10, 30 s: fire | `yes` + alert, `yes` without alert, `yes` + alert | extra: a persisting situation alerts once; a gap over 10 s alerts again |

Run one:

```sh
curl -X POST localhost:8000/demo/multi-event
```

In the response, check:

- `passed`: `true` when every step matched its expectation.
- `steps[].verdict.verdict` and `steps[].verdict.reasoning`: the VLM's decision per event.
- `steps[].verdict.evidence_event_ids`: the events the VLM saw. For `multi-event` it grows 1, 2, 3 events.
- `steps[].alert`: `null`, or the alert linked to that verdict (`verdict_id`).
- `steps[].expected_verdict`, `expected_alert`, `ok`: the scripted expectation and whether it was met.

With `jq`, a summary (event ids end in a per-run id):

```sh
curl -s -X POST localhost:8000/demo/multi-event | jq -c '{passed, steps: [.steps[] | {verdict: .verdict.verdict, evidence: .verdict.evidence_event_ids, alert: (.alert != null)}]}'
```

```json
{"passed":true,"steps":[{"verdict":"unsure","evidence":["multi-event-918d45eb-1"],"alert":false},{"verdict":"unsure","evidence":["multi-event-918d45eb-1","multi-event-918d45eb-2"],"alert":false},{"verdict":"yes","evidence":["multi-event-918d45eb-1","multi-event-918d45eb-2","multi-event-918d45eb-3"],"alert":true}]}
```

### The plain API by hand

Create a condition. The response contains the spec the VLM chose once, at creation:

```sh
curl -X POST localhost:8000/conditions -H 'Content-Type: application/json' \
  -d '{"text": "Alert me if you see smoke"}'
# {"id":1,"text":"Alert me if you see smoke","spec":{"evidence_type":"frame","history_window_seconds":null},"created_at":"..."}
```

Post an event, as the camera backend would. `note` is the simulated scene (`people`, `objects`, `observations`); the mock reads it instead of the frame. The call returns `202` and the event is processed in the background; posting the same `id` again returns `409`.

```sh
curl -X POST localhost:8000/events -H 'Content-Type: application/json' -d '{
  "id": "evt-1",
  "timestamp": "2026-10-28T12:00:00Z",
  "frame_path": "frames/evt-1.jpg",
  "clip_path": "clips/evt-1.mjpeg",
  "note": {"observations": {"smoke": "yes"}}
}'
```

List the results:

```sh
curl localhost:8000/verdicts   # filters: ?condition_id=1&verdict=unsure
# [{"id":1,"created_at":"...","condition_id":1,"event_id":"evt-1","verdict":"yes","reasoning":"Mock: evt-1 observes 'smoke': yes","evidence_event_ids":["evt-1"]}]
curl localhost:8000/alerts
# [{"id":1,"condition_id":1,"verdict_id":1,"created_at":"..."}]
```

Other endpoints: `GET /conditions`. Timestamps must include a timezone.

## Tests

```sh
uv sync
uv run pytest
```

Needs Python 3.12 and [uv](https://docs.astral.sh/uv/). The in-memory tests need nothing else: 110 passed, 34 skipped (the skipped ones are the Postgres variants).

The Postgres variants run when `TEST_DATABASE_URL` is set. Use a throwaway database; the tests drop and truncate its tables.

```sh
docker run -d --rm --name adrec-pgtest -e POSTGRES_PASSWORD=postgres -p 55432:5432 postgres:17-alpine
TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:55432/postgres uv run pytest
docker stop adrec-pgtest
```

With Postgres: 144 passed, 0 skipped. Wait a few seconds after `docker run` until `docker exec adrec-pgtest pg_isready -U postgres` reports it is accepting connections.

`tests/test_e2e.py` runs the six demo scenarios and the plain API through the real app, queue and processing loop. `tests/test_vlm.py` covers the mock's rules.

## System diagram

![System diagram](doc/system-diagram.png)

The source is `doc/system-diagram.excalidraw`.

Data flow:

1. `POST /conditions`: `ConditionService` asks the VLM once (`classify_condition`) which evidence the condition needs (`frame`, `clip` or `history` with a window in seconds) and stores the condition with that spec.
2. `POST /events` stores the event in Postgres and puts it on an in-process FIFO queue (`202`).
3. A background loop takes events in arrival order and processes each in a worker thread, so the API stays responsive.
4. For every condition, the context gatherer (deterministic code) builds the evidence from the spec: the trigger frame, the frame and clip, or the trigger frames of the events in the history window, oldest first.
5. `vlm.evaluate` judges the condition on that evidence only and returns `yes`, `no` or `unsure` with reasoning.
6. The verdict is stored with the ids of the events the VLM saw.
7. On `yes`, an alert linked to the verdict is stored and logged, unless the previous `yes` for the same condition was at most 10 s earlier (the same situation continuing).

## Main assumptions

- **Single camera.** Events and conditions have no `camera_id`; every condition is checked against every event.
- **The mocked VLM reads scene notes, not pixels.** Each event carries a `note` (people, objects, observations) as simulated ground truth; frame and clip paths are plain strings and no media files exist.
- **English keyword matching in the mock.** It picks the evidence type and rule from substrings of the condition text (for example "loiter", "package", "flee"); a real model would understand any language.
- **Events arrive roughly in order.** History is read by timestamp and later events are never used for an earlier event's verdict.
- **Cameras re-fire about every 10 s.** The 10 s alert dedup window and the mock's 20 s loitering gap limit are derived from this.
- **Conditions apply from the next event on.** Nothing is re-evaluated retroactively, and an `unsure` that no later event resolves stays `unsure`.
- **Event ids are unique** (`409` otherwise).
- **No persistence across `docker compose down`, no migrations.** Tables are created at startup. Queued events are in memory and lost on a restart.
- **No authentication.**

## Project layout

```
app/
  main.py         app wiring, event queue loop (worker thread)
  api.py          Services container and routes
  processing.py   process_event: gather, evaluate, store verdict, alert with dedup
  context.py      deterministic evidence selection (frame, clip, history window)
  conditions.py   condition creation (spec classified once by the VLM)
  vlm.py          VLM interface and MockVLM with its rule table
  domain.py       enums and Pydantic models
  demo.py         scripted scenarios and the /demo endpoints
  repositories/   interfaces, in-memory (tests) and SQLAlchemy/Postgres implementations
tests/            unit, repository contract, e2e (in-memory and Postgres)
doc/              design note, production notes
Dockerfile, docker-compose.yml
```

## Documents

- [Design note](doc/Design.md): architecture, key decisions, alternatives, mock strategy.
- [Production notes](doc/production-notes.md): prototype simplifications and what would change in production.

## AI usage

[doc/ai-usage.md](doc/ai-usage.md) (to be added)
