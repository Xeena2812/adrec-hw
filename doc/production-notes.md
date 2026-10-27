# Production notes: what would change beyond the prototype

Running list of simplifications made for the prototype and what would replace them in production. Feeds the "what I'd change in production" part of the design note.

## Infrastructure

- **Event processing:** the prototype uses a simple in-process queue consumed by a single loop. Production would use a distributed message broker (e.g. RabbitMQ) with a pool of workers consuming camera events, so ingestion and VLM inference scale independently.
- **Frame and clip storage:** the prototype stores frames and clips on the local file system, with paths in the event DB. Production would use cloud blob/object storage (e.g. S3-compatible).
- **Event storage:** the prototype uses Postgres. Production would use a proper time-series database for camera events, since nearly every query is "events for camera X in time window Y".

## API

- **Only the endpoints the pipeline and demo need:** creating and listing conditions, ingesting events, and listing verdicts and alerts. The prototype is an MVP, not a feature-complete API. A production API would also have read endpoints for single records and for events (`GET /events`, `GET /conditions/{id}`, `GET /verdicts/{id}`, `GET /alerts/{id}`), a review view of a verdict with its evidence events expanded (for human review of `unsure`), editing and deleting conditions, pagination, and a health endpoint.

## Mock VLM

- **Note for the mock VLM class (docstring/comment):** in production, the real VLM is handed all the context it needs (event frame, clip, or several earlier events), selected by deterministic pipeline code based on the condition's compiled spec. The VLM only has to return a final verdict (yes / no / unsure). The mock reads a ground-truth note per event instead of looking at pixels, but keeps the same interface, so swapping in the real Qwen model changes only that one class.

## Alternatives worth revisiting

- **Agentic retrieval loop:** instead of deterministic code selecting context, give the VLM tools to fetch related events, frames and clips in a loop until it reaches a verdict (yes / no / human review). Rejected for the prototype because it moves the decision flow into the model, makes testing mostly about replaying scripted tool calls, and multiplies inference calls per event on a local 27B model. Could be worth pursuing in production for open-ended conditions, with guardrails such as step limits and a context budget.
- **Two-stage evaluation with relevance screening:** the prototype checks every condition against every event: one context-gathering step and one full VLM call per (event, condition) pair. That is acceptable with the few conditions per camera in this scenario, but the cost grows linearly with the number of conditions. A production system could add a cheap first stage per event: one VLM call sees only the trigger frame plus the list of condition texts, returns a short description of the frame, and picks the conditions worth a closer look. Only those then get their full context gathered and a full VLM evaluation.
  - *Trade-off:* fewer full VLM calls (the expensive part, often with many frames) against the chance of silently dropping a condition that would have alerted. A condition filtered out at the screening stage gets no verdict for that event.
  - *Why the risk is manageable:* screening runs on every event, so a suspicious situation that can't be decided from one frame is still passed on to the full evaluation, which then gets the history or clip it needs. A miss on one event can be caught on the next one in the same situation.
  - *What it depends on:* how the screening prompt is phrased. It has to favour recall over precision: pick every condition that could plausibly be in progress, and treat "can't tell from this frame" as relevant. Its miss rate would need to be measured against checking every condition before relying on it; a smaller dedicated model could do the screening.
  - *Open detail:* whether conditions skipped by screening still get a verdict row (e.g. a `skipped` value with the frame description as reasoning), so reviewers can see why something didn't alert.

## Known limitations

- **Person re-identification:** for multi-event conditions (loitering, package left then person leaves), the prototype relies on the VLM's descriptions to judge whether it's the same person across events. This is unreliable. Production would need dedicated re-identification or tracking across events.
- **Per-condition evaluation logic:** the prototype uses one generic evaluation flow for all conditions. More precise, condition-specific metrics are out of scope for this size of task.
