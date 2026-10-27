# CCTV Event Analytics Backend: design document

Design for a take-home task: a Dockerized prototype of an analytics backend that checks free-text user alert conditions against incoming CCTV camera events. The vision-language model (a ~27B Qwen VLM in the real product) is mocked.

This document is the starting point for implementation. Production-only changes and rejected alternatives live in `production-notes.md`.

---

## How to use this document (instructions for Claude Code)

- The **Settled** sections are decisions I've made. Implement them as written. If one looks wrong or contradicts another, say so before changing it.
- The **Open questions** section lists what I haven't decided. **Before writing code that depends on an open question, ask me.** Don't fill these in silently.
- Keep the VLM behind an interface, so the mock can be swapped for the real model by changing one class.
- Prefer small, testable components over one large handler.
- Keep a short log of which parts you generated and which I changed (needed for the AI-usage deliverable).

---

## 1. The task in brief

- Users attach free-text conditions to a camera, e.g. "alert me if you see smoke", "alert me if someone loiters in front of the embassy", "alert me if someone leaves a package at the door and walks away".
- The camera system has a simple built-in detector (motion, person, vehicle). It sends a **camera event** with: event ID, camera ID, timestamp, trigger frame, and a 10-second MJPEG clip around the frame.
- If a situation persists, a new event fires every ~10 s, so one real happening can appear as a chain of events.
- The backend decides, per event, whether each condition on that camera is met. If yes, it records an alert linked to the relevant camera event(s), with analytic info.
- Conditions differ in the evidence they need: a single frame (smoke), the clip (someone fleeing), several events over time (loitering), or an ordered sequence (package left, then person leaves).

**Required demo cases:**
1. A condition decidable from a single frame.
2. A condition that needs several consecutive events or history.
3. A case where information is insufficient for a confident decision.
4. A negative case: an event fires but the condition is not met.

**Deliverables:** source code, Dockerfile (easy start), README (start-up, assumptions, solution overview), system diagram, reproducible demo and automated tests, design note (decisions, alternatives, production changes), AI-usage note.

**Out of scope:** real RTSP streams, running the real model or a GPU, a production-grade web UI, Kubernetes or multi-machine setups, a full VMS.

---

## 2. Architecture (settled)

### External to the analytics backend
- **Camera backend** (simulated): the camera, the **Event DB** where events are stored, and a **file system** holding frames and clips.
- **Conditions DB:** user-defined conditions plus their compiled specs.
- **Decisions DB:** verdict log.
- **Alert / notification service:** receives alerts. Mocked, e.g. it logs or prints them.
- **Simple UI:** lets a user define conditions. Minimal; a production UI is not required.

### Inside the analytics backend
| Component | Responsibility |
|---|---|
| **Event queue** | In-process FIFO queue. The camera backend pushes event data into it. Order matters for temporal conditions, so not a stack. |
| **Event processor** | Main loop. Pops an event, loads the conditions for that camera, and for each one runs context gathering and the VLM, stores the verdict, and applies the **alert logic** (new alert vs. same situation continuing, by checking the Decisions DB for a recent "yes" on the same condition). |
| **Condition compiler** | Runs **once, when a condition is created**. Turns the free-text condition into a structured spec (see §4). Mocked. |
| **Context gatherer** | Deterministic code. Given an event and a condition's spec, reads the Event DB and file system and assembles the evidence for the VLM (current frame, clip, and/or earlier events). |
| **VLM interface + Mock VLM** | Takes the gathered context and the condition and returns a verdict. The mock is one implementation behind the interface. |
| **Notifier interface** | Sends alerts to the alert/notification service. |

Repository classes for the three DBs exist in code but aren't shown on the architecture diagram.

---

## 3. Core design decisions (settled)

### VLM boundary
- Deterministic code (the context gatherer) decides which context is sent to the VLM.
- The VLM only judges what it's handed and returns **yes / no / unsure**, plus its reasoning.
- The VLM never asks for more context. An agentic retrieval loop was considered and rejected; see `production-notes.md`.

### Conditions are compiled once, at creation time
- The evidence a condition needs doesn't change between events, so it's decided once when the condition is created, not per event.
- *Rejected alternative:* an LLM call per event to decide which evidence a condition needs.

### Stateless re-evaluation
- Every new event triggers a fresh evaluation of each condition over the current event plus its relevant history. History is already in the Event DB, so no pending or retry state is stored.
- An "unsure" verdict is simply logged. A later event may produce a definitive answer over the extended history.
- *Rejected alternative:* on "unsure", wait for N more events or request more frames, then escalate. Rejected for the complexity of storing and revisiting pending decisions.

### Episodes from event gaps
- The camera re-fires every ~10 s while a situation persists, so back-to-back events likely belong to the same happening. A large gap (e.g. 2 minutes) means unrelated events.

### One verdict row per (event, condition) pair
- *Rejected alternative:* one row per event with a list of conditions, since each condition gets its own verdict.

---

## 4. Data model (draft)

**events** (owned by the camera backend; the analytics backend reads it)
- id, camera_id, timestamp, frame_path, clip_path

**conditions**
- id, camera_id, text (user's free-text condition)
- compiled spec: evidence type, history window, priority (exact fields: see open question 1)

**verdicts** (Decisions DB)
- id, timestamp, condition_id, event_id, verdict (yes / no / unsure), reasoning
- Still open: how a verdict based on several events records them all (see open question 2).

**alerts:** structure still open (see open question 2).

---

## 5. Main flows (settled shape)

### Condition creation
1. The user enters a free-text condition via the simple UI.
2. The condition compiler (mock LLM) produces the structured spec.
3. The condition and spec are stored in the Conditions DB.

### Event processing
1. The camera backend stores the event in the Event DB and media in the file system, and pushes event data into the event queue.
2. The event processor pops the event and loads all conditions for that camera.
3. For each condition:
   1. The context gatherer assembles evidence according to the condition's spec, reading earlier events from the Event DB and media from the file system as needed.
   2. The VLM returns a verdict and reasoning.
   3. The verdict is stored in the Decisions DB.
   4. Alert logic: if the verdict is "yes" and there's no recent "yes" for the same condition, an alert is created and sent via the notifier.

---

## 6. Mock strategy (settled)

### Mock VLM
- The pipeline passes frame and clip paths exactly as if the model were real.
- The mock ignores the pixels. It reads a **hand-written ground-truth note per event**, e.g. a sidecar file next to the frame, and derives the verdict from it.
- The note can state verdicts explicitly (e.g. `loitering: unsure`), so every demo outcome, including "unsure", is scripted and deterministic.
- The mock class must carry a docstring/comment explaining that in production the real VLM receives all necessary context, selected by deterministic code from the condition's spec, and only returns a final verdict.
- Realistic images are not required, since the mock never looks at pixels.

### Mock condition compiler
- Returns a fixed spec per demo condition, or picks one from a few keywords, with a sensible default for anything else.

---

## 7. Open questions (ask me before implementing these)

1. **How context is chosen per condition.** Options:
   - (a) The mock compiler picks one of three fixed options (event frame only / frames before the event / event + clip).
   - (b) The user declares the option, e.g. a dropdown. More agency, but weakens the natural-language idea.
   - (c) Hybrid: the compiler suggests, the user can override.
   - Also to clarify: does "frames before the event" mean frames from the same clip, or *previous events*? Loitering needs previous events.
2. **Alert record.** Separate table, or a "yes" verdict? How does one alert link to several events? What analytic info does it carry?
3. **Dedup window.** How "recent" a previous "yes" must be to count as the same situation: a time window, the current episode, or something else. To be set as a parameter while coding.
4. **Episode gap threshold.** The exact gap that ends an episode, and whether the history window is "the current episode" or "the last N minutes".
5. **Silent unsure.** If no further events arrive, an "unsure" stays unsure. Is that acceptable, or should something escalate it to human review?
6. **UI vs. API.** How minimal the UI is. Would API docs plus a demo replay script be enough?
7. **Storage tech.** Postgres with file paths (current plan) vs. storing images in the DB (e.g. MongoDB).
8. **Stack details.** Web framework, how the camera backend simulator pushes events, and how the demo is triggered.

---

## 8. Next steps

1. Settle open question 1. Note the rejected alternatives.
2. Map the four demo cases to concrete event sequences, ground-truth notes and conditions. These become the seed data and the test suite.
3. Implement the components in §2, starting with the VLM interface and mock, the context gatherer, and the event processor.
4. Write tests for each demo case, plus unit tests for the alert logic.
5. Docker setup, README, design note (from this document plus `production-notes.md`), system diagram export, AI-usage note.
