# Production notes: what would change beyond the prototype

Running list of simplifications made for the prototype and what would replace them in production. Feeds the "what I'd change in production" part of the design note.

## Infrastructure

- **Event processing:** the prototype uses a simple in-process queue consumed by a single loop. Production would use a distributed message broker (e.g. RabbitMQ) with a pool of workers consuming camera events, so ingestion and VLM inference scale independently.
- **Frame and clip storage:** the prototype stores frames and clips on the local file system, with paths in the event DB. Production would use cloud blob/object storage (e.g. S3-compatible).
- **Event storage:** the prototype uses Postgres. Production would use a proper time-series database for camera events, since nearly every query is "events for camera X in time window Y".

## Mock VLM

- **Note for the mock VLM class (docstring/comment):** in production, the real VLM is handed all the context it needs (event frame, clip, or several earlier events), selected by deterministic pipeline code based on the condition's compiled spec. The VLM only has to return a final verdict (yes / no / unsure). The mock reads a ground-truth note per event instead of looking at pixels, but keeps the same interface, so swapping in the real Qwen model changes only that one class.

## Alternatives worth revisiting

- **Agentic retrieval loop:** instead of deterministic code selecting context, give the VLM tools to fetch related events, frames and clips in a loop until it reaches a verdict (yes / no / human review). Rejected for the prototype because it moves the decision flow into the model, makes testing mostly about replaying scripted tool calls, and multiplies inference calls per event on a local 27B model. Could be worth pursuing in production for open-ended conditions, with guardrails such as step limits and a context budget.

## Known limitations

- **Person re-identification:** for multi-event conditions (loitering, package left then person leaves), the prototype relies on the VLM's descriptions to judge whether it's the same person across events. This is unreliable. Production would need dedicated re-identification or tracking across events.
- **Per-condition evaluation logic:** the prototype uses one generic evaluation flow for all conditions. More precise, condition-specific metrics are out of scope for this size of task.
