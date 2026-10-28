# Production notes

The prototype's simplifications and what would replace them in production. [Design.md](Design.md) describes what was built.

## Scope and infrastructure

- **Single camera.** There is no `camera_id`, so every condition is checked against every event. Production adds it to events and conditions, loads only the event's camera's conditions, and scopes the history query to that camera.
- **In-process queue.** A single loop consumes it, and queued events are lost on a restart. Production would use a message broker (e.g. RabbitMQ) with a pool of workers, so ingestion and VLM inference scale independently and unacknowledged events are redelivered.
- **No media files.** Frame and clip paths are plain strings, because the mock never reads them. Production would keep the media in object storage (e.g. S3-compatible).
- **Postgres for events.** Nearly every query is "events for camera X in window Y", which suits a time-series database.
- **Late events.** History is read by timestamp up to the current event, but a late, out-of-order event doesn't cause earlier verdicts to be revisited. Production needs a policy for this: a reorder buffer, or re-evaluating the affected window.
- **No migrations and no persistent data.** Tables are created at startup. Postgres has no named volume, so after `docker compose down` the next `up` starts empty. Production would use migrations (e.g. Alembic) and persistent storage.

## API

- **`POST /events` stands in for the camera backend**, which in the real system owns the events table and the media.
- **`/demo` endpoints exist only for the demo.** Swagger at `/docs` is the "simple UI".
- **Minimal API surface.** There is no authentication, no editing or deleting of conditions, no single-record or event endpoints, no pagination and no health endpoint. Production also needs a review view that shows a verdict with its evidence events, for human review of `unsure`.
- **Conditions apply from the next processed event on.** They are not evaluated retroactively against stored events.

## Mock VLM

- **The mock sits behind the real interface.** Swapping in the real Qwen model replaces `MockVLM` and the line in `app/main.py` that creates it. The real model would receive the same context, chosen by deterministic code from the condition's spec, and return only a verdict with reasoning.
- **Keyword matching is English-only and substring-based.** A real model understands any language, including the task's Hungarian examples. The mock misclassifies freely phrased conditions.
- **Scene notes on the event.** The `note` field is simulation-only ground truth, stored with the event for convenience. Production removes it.
- **Time between events.** The mock's loitering rule uses the timestamps in the context: a gap over 20 s breaks a person's run. A real VLM would get the frame timestamps in its prompt; how well it weighs them depends on the prompt and needs evaluating.

## Alerts

- **One alert links to one verdict.** A `yes` that dedup suppresses is stored but not attached to the alert. Production could aggregate a situation into one alert with first and last event, duration and event count.
- **The dedup window is a fixed 10 s.** It shows the intent. Production would tune it (e.g. 2–3x the camera re-fire interval) or set it per condition.
- **The alert service is a log line.** In production it is an external service.
- **Unresolved `unsure` stays `unsure`.** Production would route it to human review; the verdict's evidence events make that possible.

## Alternatives worth revisiting

- **Agentic retrieval loop.** The VLM would get tools to fetch related events and clips until it reaches a verdict. It was rejected for the prototype because it moves control flow into the model, makes tests mostly replays of scripted tool calls, and multiplies inference calls on a local 27B model. It may suit open-ended conditions, with step limits and a context budget.
- **Structured JSON output.** The model would generate JSON against a schema, enforced by schema-constrained decoding and validated with the same Pydantic models. For evaluation the fields could be a confidence score, the people and objects per frame, when the condition starts to hold, and bounding boxes. For classification they could be the spec plus a short checklist of what to look for. This would give alerts richer analytic information and make model quality measurable. An output that stays invalid after a retry becomes `unsure`.
- **Two-stage evaluation.** Today every condition gets a full VLM call on every event, so the cost grows with the number of conditions. A cheap first stage per event would show the model the trigger frame and the list of condition texts, and pick the conditions worth a full evaluation.
  - *The risk* is silently skipping a condition that would have alerted. Screening must therefore favour recall, treating "can't tell from this frame" as relevant, and its miss rate needs measuring first.
  - *What softens the risk:* screening runs on every event, so a miss can be caught on the next event of the same situation.

## Known limitations

- **Person re-identification.** Multi-event conditions depend on knowing it's the same person across events. The mock uses person ids; a real VLM judging from descriptions is unreliable, so production needs dedicated tracking or re-identification.
- **One generic pipeline.** Only the mock has per-condition rules; condition-specific metrics are out of scope.
