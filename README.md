# CCTV event analytics backend

A prototype backend that checks free-text user alert conditions against CCTV camera events. A camera event carries a trigger frame and a short clip, and re-fires every 10 s while a situation persists. For each event, every condition is judged by a vision-language model (VLM) as `yes`, `no` or `unsure`, with reasoning. A `yes` raises an alert linked to its verdict and evidence events. The ~27B Qwen VLM is mocked: it reads a hand-written scene note per event instead of pixels. FastAPI + Postgres, started with Docker Compose.

## Quick start

```sh
docker compose up --build
```

The API listens on http://localhost:8000, and the Swagger UI at http://localhost:8000/docs is the "simple UI". Alerts are also logged by the `api` container (`ALERT ...` lines).

`docker compose down` deletes all data (Postgres has no volume).

## Demo

`POST /demo/{case}` creates the case's condition, posts its events through the normal pipeline (event queue, processing loop, Postgres), waits for the verdicts and returns them next to the expected outcome. Cases can be run in any order and repeated; each run's events are placed after all stored events, so earlier runs never fall into a history window (timestamps therefore drift into the future on repeated runs).

| Case             | Condition                                                       | Events (time, what the scene note shows)               | Verdicts                                                | Covers                                                                  |
| ---------------- | --------------------------------------------------------------- | ------------------------------------------------------ | ------------------------------------------------------- | ----------------------------------------------------------------------- |
| `single-frame` | Alert me if you see smoke                                       | 0 s: smoke                                             | `yes`, alert                                          | 1. decidable from a single frame                                        |
| `multi-event`  | Alert me if someone loiters in front of the embassy             | 0, 10, 20 s: the same person `p1`                    | `unsure`, `unsure`, `yes` (alert on the third)    | 2. needs several events: the history grows with each event              |
| `package-left` | Alert me if someone leaves a package at the door and walks away | 0 s: person `p2` with a package; 10 s: package alone | `no`, `yes` (alert)                                 | 2. order matters: the earlier event shows who walked away               |
| `insufficient` | same package condition                                          | 0, 10 s: package visible, nobody in any frame          | `unsure`, `unsure`; no alert                        | 3. not enough information to decide                                     |
| `negative`     | Alert me if someone is fleeing                                  | 0 s: the event fires, the scene shows no fleeing       | `no`, no alert                                        | 4. event fires, condition not met                                       |
| `dedup`        | Alert me if you see a fire                                      | 0, 10, 30 s: fire                                      | `yes` + alert, `yes` without alert, `yes` + alert | extra: a persisting situation alerts once; a gap over 10 s alerts again |

To run a case, open <http://localhost:8000/docs>, expand `POST /demo/{case}`, click **Try it out**, pick a case and click **Execute**. The response lists each event with its verdict, the reasoning, the events the VLM saw (`evidence_event_ids`) and the alert, if any, next to the expected outcome. `passed` is `true` when they all match. `GET /verdicts` and `GET /alerts` list everything stored so far.

Your own conditions and events can be posted with `POST /conditions` and `POST /events`. The mock reads the event's `note` instead of pixels, so for a new condition, put a word from the condition text in `note.observations`: for "Alert me if a car parks in the driveway", post an event with `"note": {"observations": {"car": "yes"}}`.

## Tests

```sh
uv sync
uv run pytest
```

Needs Python 3.12 and [uv](https://docs.astral.sh/uv/). The in-memory tests need nothing else.

The Postgres variants run when `TEST_DATABASE_URL` is set. Use a throwaway database; the tests drop and truncate its tables.

```sh
docker run -d --rm --name adrec-pgtest -e POSTGRES_PASSWORD=postgres -p 55432:5432 postgres:17-alpine
TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:55432/postgres uv run pytest
docker stop adrec-pgtest
```

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

## Documents

- [Design note](doc/Design.md): architecture, key decisions, alternatives, mock strategy.
- [Production notes](doc/production-notes.md): the prototype's assumptions and simplifications, and what would change in production.

## AI usage

[doc/ai-usage.md](doc/ai-usage.md) (to be added)
