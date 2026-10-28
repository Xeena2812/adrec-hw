# Production notes: what would change beyond the prototype

Simplifications made for the prototype and what would replace them in production. The design note ([Design.md](Design.md)) describes what was built; this file is the "what I'd change in production" part.

## Scope

- **Single camera:** there is no `camera_id` on events or conditions, so every condition is checked against every event, and the history query reads all events in the window. Production adds `camera_id` to events and conditions, loads only the conditions of the event's camera, and scopes the history query to that camera.

## Infrastructure

- **Event processing:** the prototype uses a simple in-process queue consumed by a single loop. Production would use a distributed message broker (e.g. RabbitMQ) with a pool of workers consuming camera events, so ingestion and VLM inference scale independently.
- **Frame and clip storage:** the prototype has no media files; frame and clip paths are plain strings on the event, since the mock never reads them. Production would keep the media in cloud blob/object storage (e.g. S3-compatible) with the paths in the event DB.
- **Event order:** events are processed in arrival order, and the history for an event is read by timestamp up to that event, so later events never influence an earlier verdict. A late, out-of-order event gets a verdict over the history stored so far, but earlier verdicts are not revisited, and the dedup check compares with the most recently stored `yes`. Production would need a policy for late events (a reorder buffer, or re-evaluating the affected window).
- **Schema and data:** tables are created at startup with no migrations, and Postgres has no volume, so `docker compose down` deletes everything. Production would use migrations (e.g. Alembic) and persistent storage.
- **Events queued in memory:** events waiting in the in-process queue are lost if the process restarts (they remain in the events table but are not re-queued). The broker above fixes this: unacknowledged messages are redelivered.
- **Event storage:** the prototype uses Postgres. Production would use a proper time-series database for camera events, since nearly every query is "events for camera X in time window Y".

## API

- **`POST /events` stands in for the camera backend pushing events.** In the real system the camera backend owns the events table and the media files; the analytics backend would read events from it and receive them from the broker.
- **`/demo` endpoints** (`POST /demo/{case}`) are mounted in the app only for the demo. Production would drop them, or keep the scenarios as test fixtures.
- **No authentication or authorization.** Production would authenticate users and the camera backend, and scope conditions, verdicts and alerts to their owners.
- **Conditions apply from the next event on.** A new condition is not evaluated retroactively against stored events.
- **The `/docs` Swagger page is the "simple UI".** A production UI for defining and reviewing conditions, alerts and verdicts is out of scope.
- **Only the endpoints the pipeline and demo need:** creating and listing conditions, ingesting events, and listing verdicts and alerts. The prototype is an MVP, not a feature-complete API. A production API would also have read endpoints for single records and for events (`GET /events`, `GET /conditions/{id}`, `GET /verdicts/{id}`, `GET /alerts/{id}`), a review view of a verdict with its evidence events expanded (for human review of `unsure`), editing and deleting conditions, pagination, and a health endpoint.

## Mock VLM

- **Note for the mock VLM class (docstring/comment):** in production, the real VLM is handed all the context it needs (event frame, clip, or several earlier events), selected by deterministic pipeline code based on the condition's spec. The VLM only has to return a final verdict (yes / no / unsure). The mock reads a scene note per event instead of looking at pixels, but keeps the same interface, so swapping in the real Qwen model changes only that one class.
- **Keyword matching is English-only and substring-based.** A real model understands any language, including the Hungarian example prompts of the task. The mock's keyword list ("loiter", "package", "run", ...) is only a demo stand-in and misclassifies freely phrased conditions.
- **Scene notes on the Event model.** The `note` field (people, objects, observations) is simulation-only ground truth, carried on the event and stored in the events table for convenience. Production removes it; the VLM reads the pixels of the frames and clip.
- **Time between events.** The mock uses the timestamps in the context: in the loitering rule a gap of more than 20 s between adjacent frames breaks a person's run. A real VLM would get the frame timestamps in its prompt and weigh them; how well it does depends on the prompt and would need to be evaluated.

## Alerts

- **One alert links to one verdict.** A deduplicated continuation (a `yes` inside the dedup window) is stored as a verdict but is not attached to the alert, and the alert service is treated as external, with a log line standing in for it. Production could aggregate a situation into one alert with first and last event, duration and event count.
- **Dedup window:** a fixed 10 s, measured from the previous `yes` of the same condition (alerting or not) and equal to the camera re-fire interval. It shows the intent. Production would tune it (for example 2-3x the re-fire interval) or make it per condition.
- **Unresolved `unsure`:** an `unsure` that no later event resolves stays `unsure`. Production would route it to human review; the verdict's evidence events make that possible.

## Alternatives worth revisiting

- **Agentic retrieval loop:** instead of deterministic code selecting context, give the VLM tools to fetch related events, frames and clips in a loop until it reaches a verdict (yes / no / human review). Rejected for the prototype because it moves the decision flow into the model, makes testing mostly about replaying scripted tool calls, and multiplies inference calls per event on a local 27B model. Could be worth pursuing in production for open-ended conditions, with guardrails such as step limits and a context budget.
- **Structured JSON output from the VLM:** the VLM returns a verdict with free-text reasoning (`VLMResult`), and its classification returns a `ConditionSpec`. The model could instead be asked to generate JSON against a schema, enforced by schema-constrained decoding in the model server and validated with the same Pydantic models. Possible fields:
  - for evaluation: a confidence score, the people and objects seen in each frame, the frame where the condition starts to hold, and bounding boxes;
  - for classification: the evidence type and window, plus a short checklist of what to look for.

  This would give alerts richer analytic information and make the outputs machine-checkable. It would also make measuring model quality easier. An output that is still invalid after a retry would fall back to `unsure`.
- **Two-stage evaluation with relevance screening:** the prototype checks every condition against every event: one context-gathering step and one full VLM call per (event, condition) pair. That is acceptable with the few conditions per camera in this scenario, but the cost grows linearly with the number of conditions. A production system could add a cheap first stage per event: one VLM call sees only the trigger frame plus the list of condition texts, returns a short description of the frame, and picks the conditions worth a closer look. Only those then get their full context gathered and a full VLM evaluation.
  - *Trade-off:* fewer full VLM calls (the expensive part, often with many frames) against the chance of silently dropping a condition that would have alerted. A condition filtered out at the screening stage gets no verdict for that event.
  - *Why the risk is manageable:* screening runs on every event, so a suspicious situation that can't be decided from one frame is still passed on to the full evaluation, which then gets the history or clip it needs. A miss on one event can be caught on the next one in the same situation.
  - *What it depends on:* how the screening prompt is phrased. It has to favour recall over precision: pick every condition that could plausibly be in progress, and treat "can't tell from this frame" as relevant. Its miss rate would need to be measured against checking every condition before relying on it; a smaller dedicated model could do the screening.
  - *Open detail:* whether conditions skipped by screening still get a verdict row (e.g. a `skipped` value with the frame description as reasoning), so reviewers can see why something didn't alert.

## Known limitations

- **Person re-identification:** for multi-event conditions (loitering, package left then person leaves), the real VLM would have to judge from descriptions whether it's the same person across events, which is unreliable. The mock sidesteps this with person ids in the scene notes. Production would need dedicated re-identification or tracking across events.
- **Per-condition evaluation logic:** the pipeline is the same for all conditions, and only the mock has per-kind rules. More precise, condition-specific metrics are out of scope for this size of task.
