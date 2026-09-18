# Design

Decided contracts for `callva-livekit`. This is the reference the implementation
follows; every number and API name here was verified against `livekit-agents` 1.5.7,
`livekit-protocol` 1.1.7 and the `livekit/livekit` server sources.

## 1. Scope

Two independent capabilities that any LiveKit agent can opt into:

- **Webhook** — emit a `call.started` webhook when the call goes live and a `call.ended`
  webhook when it finishes, carrying the transcript, usage, recording, session data, and
  everything that went wrong along the way.
- **Config** — resolve per-call configuration before the session starts, from agent
  dispatch metadata or from an external endpoint: the agent, its prompt and greeting, the
  limits the call runs under, and the speech stack it is built from.
- **Call** — wait until an outbound call is actually answered rather than merely ringing,
  end one that has run too long or that nobody is on any more, and hang up in a way that
  releases the caller rather than only the agent.

Each is armed by its own call. Installing the package activates nothing.

### Non-goals

- **Dispatch.** Placing a call is a server-API conversation between the platform and
  LiveKit, and it happens before the agent process exists. Out of scope by construction.
- **Provider configuration.** The public config schema carries no speech-stack, model or
  voice settings. Vendor-specific material travels in the opaque `extra` field.
- **Speaking to the caller.** The `call` module ends a call; it never makes the agent say
  anything, because how an agent speaks depends on its model. Reminder prompts and
  transfer stay out for that reason.

## 2. Names

The distribution is `callva-livekit` and it imports as `callva.livekit`, following the
convention LiveKit itself uses: `livekit-agents` imports as `livekit.agents`,
`livekit-plugins-openai` as `livekit.plugins.openai`. The distribution name is the import
path with dashes, so `pip install` and `import` never disagree. A later split keeps the
property: `callva-livekit-webhook` would import as `callva.livekit.webhook`.

Environment variables carry no vendor prefix — `WEBHOOK_URL`, `CONFIG_URL`,
`RECORDING_S3_BUCKET`. This is an extension to the Agents SDK; that a URL points at CallVA
is configuration, not identity. The same reasoning applies to the delivery headers
(`X-Webhook-Signature`, `X-Webhook-Event`) and to the event names themselves
(`call.started`, `call.ended`).

The one deliberate exception is the `callva` key inside `ctx.job.metadata`. That key exists
to keep our block from colliding with the host application's own metadata, and being
distinctive is the entire job it does. A generic name there would defeat it.

## 3. Layout

```
callva/                     PEP 420 namespace, no __init__.py
  livekit/                  PEP 420 namespace, no __init__.py
    core/                   identity, per-job state, HTTP transport, logging
    config/                 config resolution and templating
    webhook/                event delivery, recording upload
    call/                   being answered, watched while it runs, and hung up
```

One distribution, four modules. Because `callva` and `callva.livekit` are namespace
packages, a module can later be split into its own distribution without changing a single
user-facing import. That makes the single-distribution choice reversible; starting with
three and merging would not be.

`core` holds what more than one module needs: the resolved call identity, the resolved
config, the HTTP client and the logger. Modules never import each other — they read and
write the per-job state that `core` owns, keyed off the ambient `JobContext`. This is how
configuration reaches future modules without being passed by hand.

## 4. Integration surface

Primary form, documented first:

```python
from livekit.agents import AgentServer, AgentSession
from callva.livekit import config as callva_config
from callva.livekit import webhook as callva_webhook

server = AgentServer()

@server.rtc_session(on_session_end=callva_webhook.on_session_end)
async def entrypoint(ctx):
    await ctx.connect()
    cfg = await callva_config.load()
    session = AgentSession(...)
    callva_webhook.attach(session)
    await session.start(agent=Agent(instructions=cfg.prompt), room=ctx.room)
```

No monkey-patching. Neither call takes a `JobContext`: both resolve it through the public
`livekit.agents.get_job_context()`, a contextvar set for the duration of the job.

Minimal form, for agents still built on `WorkerOptions`, where `on_session_end` is not
reachable:

```python
cfg = await callva_config.load()
callva_webhook.attach(session)
```

`attach()` falls back to `ctx.add_shutdown_callback()`. That path works, but see §5 for
why it is the secondary recommendation.

Nothing is substituted behind the caller's back. `load()` returns an object; using the
prompt is the caller's decision.

## 5. Lifecycle and ordering

Verified in `livekit/agents/ipc/job_proc_lazy_main.py:360-411`:

```
shutdown signalled
entrypoint task finishes                 (15s grace, then cancelled)
session.aclose()                         RecorderIO finalizes the OGG
on_session_end(ctx)                      ← primary hook, budget = session_end_timeout (300s)
ctx._on_session_end()                    SDK uploads its own report to LiveKit Cloud
room.disconnect()
shutdown callbacks                       ← fallback hook, gathered concurrently
_on_cleanup()                            session directory deleted
```

The end webhook and the recording upload run in `on_session_end`. Reasons:

- **Budget.** `session_end_timeout` defaults to 300s. Shutdown callbacks are bounded by
  `shutdown_process_timeout` (default **10s**), after which the supervisor sends a dump
  signal and kills the process (`ipc/supervised_proc.py:279-301`). A recording upload does
  not reliably fit in 10s.
- **Ordering.** It runs before `room.disconnect()`, so room and participant state are
  still readable.
- `shutdown_process_timeout` is set by the host application when it builds the worker. The
  plugin runs inside the job process and cannot raise it.

When `attach()` has to use the fallback path it logs one warning at WARNING level naming
the risk and both fixes. Silent truncation is not acceptable here.

`ctx.make_session_report()` raises if `RecorderIO` is still recording. Both hook points sit
after `session.aclose()`, so both are safe.

## 6. Call identity

Derived by the package. Who is on the call and which way it goes are never taken from the
config; what to file it under may be, because filing is the one part that belongs to
whoever keeps the records.

- **`call_id`** — a UUID, stable across every event, and the object key for the recording
  and the transcript. Assigned by the package unless someone who already has a record for
  this call names it: the dispatch metadata envelope first, then the config answer's
  `call_id`. A config that names the call after an event has gone out is refused with a
  warning — the consumer has already matched on the old id, and changing it then would
  split one call across two records.

  This is what lets an inbound call be one row in two systems. An outbound call is named
  before it is placed, so its dispatcher can pin the id; an inbound call is not placed by
  anyone, and the configuration request is the first moment a platform hears of it. A
  responder that creates its record there can hand back the id, and from then on both
  sides address one record by one id, with no field on either side holding the other's.
- **`direction`** — resolved as: the dispatch metadata envelope → an explicit argument or
  environment variable → `"inbound"`. The default is sound rather than a guess: an outbound
  call is always dispatched by someone, so it always carries metadata. Never inferred from
  participant state.
- **`from` / `to`** — read from the SIP participant attributes: `sip.phoneNumber` is the
  remote party, `sip.trunkPhoneNumber` the local one, swapped by direction. Absent for
  non-SIP sessions.

The full `sip.*` attribute set is forwarded verbatim; it is never reshaped.

## 7. Config

### Channel

**Agent dispatch metadata only**, read as `ctx.job.metadata`. Room metadata is not used.

`job.metadata` is available in the entrypoint before `ctx.connect()`, is addressed to one
specific dispatch rather than shared across the room, is capped at 512 KiB, and — unlike
room metadata and participant attributes — is delivered over the worker's own websocket and
is **not broadcast to other participants**. Putting a prompt in room metadata exposes it to
every client in the room, including browsers.

Requires explicit dispatch: the worker must have `agent_name` set. With automatic dispatch
`job.metadata` arrives empty.

### Envelope

Job metadata is a free-form string the host application may already be using. The envelope
is therefore looked for under a ``callva`` key, and a top-level object is only claimed when
it carries keys that are unambiguously ours:

```json
{ "callva": { "call_id": "…", "direction": "outbound",
              "config": { }, "config_url": "…", "webhook": { } } }
```

Metadata that is not JSON, or is JSON that belongs to someone else, is left alone and
forwarded to a configuration endpoint unchanged.

### Resolution order

```
job.metadata carries a body      → use it, returns before connect()
job.metadata carries a pointer   → follow it
job.metadata empty, env URL set  → follow that
otherwise                        → no config
```

A pointer is an endpoint, or a local file: `file://…` or a plain path, distinguished by the
absence of a scheme. A file is read as it is — no request, no waiting — which makes it the
shortest development loop. It cannot answer per caller, so it is not the production channel.

Only the endpoint path needs the SIP envelope to build its request, so only it awaits the
participant. `load()` returns immediately for a body in metadata and for a file, and after
participant join for an endpoint. Documented, not hidden.

### Request payload

The request is itself the question — the responder decides what to return from it. This is
the main inbound scenario: choose the agent by the number that was dialled.

```json
{
  "room": "...", "job_id": "...", "dispatch_id": "...", "agent_name": "...",
  "direction": "inbound",
  "from": { "number": "..." }, "to": { "number": "..." },
  "sip": { ... },
  "participant_identity": "...",
  "metadata": "<raw job.metadata, if any>"
}
```

### Response

```json
{
  "call":  { "id": "...", "direction": "inbound",
             "project_id": "...", "tenant_id": "...", "type": "..." },
  "environment": "production",
  "agent": {
    "id": "...", "name": "...",
    "prompt": "...", "greeting": "...",
    "greeting_type": "message", "agent_waits_for_user": false,
    "prompt_variables": { "name": "Anna", "attempt": 2, "vip": true },
    "max_duration_seconds": 600, "user_silence_timeout_seconds": 15,
    "call_silence_timeout_seconds": 30, "max_prompt_attempts": 2,
    "user_prompt_phrases": [ "..." ], "farewell_type": "message", "farewell": "..."
  },
  "preset":   { "name": "...", "config": { } },
  "tools":    { },
  "services": { "webhook": { "url": "...", "secret": "..." }, "metrics": { } },
  "extra":    { }
}
```

Nested, and every value has exactly one home. Nothing is repeated at the root for the
convenience of a reader, because two homes for one fact is a disagreement waiting to
happen: the prompt, the greeting and the variables belong to the agent, the id belongs to
the call, the webhook belongs to the services.

The split that matters is `preset` against everything else. `preset` describes the speech
stack and is the only block that changes when the stack does — native audio today, a
plain STT/LLM/TTS pipeline tomorrow. An agent that assembles its own pipeline ignores it
entirely and still gets a prompt, a greeting, the limits the call runs under and somewhere
to report; an agent assembled from configuration reads all of it. That is what makes the
shape worth standing on: the part that varies is quarantined in one block.

Opening the call is three states, not two. `agent_waits_for_user` says whether the agent
opens at all; `greeting_type` says whether `greeting` is a line to speak or an instruction
to compose one from. Collapsing those into "a string or nothing" loses the agent that
should speak first in its own words.

Durations are seconds, and zero means no limit — the platform's own spelling, normalised
to `None` on the way in so a caller never has to know that.

`services.webhook` overrides the environment, because multi-tenancy is resolved per call
while the environment is a deployment default.

`extra` is for what this schema does not describe. It is opaque and never interpreted.

The responder's own values come back in the report — the `agent` block verbatim, the
`environment`, and the identifiers in `call`. A platform that resolves something per call
(which webhook a customer's report is forwarded to, say) has to read back the value that
was in force for *that* call; its database holds the one before the override.

### Templating

`{{ name }}` placeholders are substituted into **both** `prompt` and `greeting` from
`variables`. A missing key is left in place verbatim and logged at WARNING. Rendering never
raises and never evaluates code. The object exposes the raw and the rendered form of each.

JSON types in `variables` are preserved — a number stays a number — and typed accessors are
provided. Substitution uses the string form at render time.

### Failure

A failed or non-2xx config request **terminates the call** with the reason logged. An agent
without its prompt is a broken call either way; failing loudly beats failing quietly. The
response status and body are logged. Overridable for callers who prefer to continue.

## 8. Webhooks

One thin envelope; everything LiveKit produces is nested verbatim so that new SDK fields
reach consumers without a release here.

```json
{
  "event": "call.started",
  "id": "<idempotency key>",
  "timestamp": 0,
  "call": {
    "id": "...", "direction": "inbound",
    "from": { "number": "..." }, "to": { "number": "..." },
    "started_at": 0, "ended_at": 0, "duration": 0, "status": "...",
    "project_id": "...", "tenant_id": "...", "type": "..."
  },
  "agent": {},
  "environment": "production",
  "livekit": {
    "room": {}, "job": {}, "participant": {}, "sip": {},
    "session_report": {}
  },
  "recording": {},
  "tags": {}
}
```

`agent` and `environment` are configuration's, echoed back untouched: the response's whole
`agent` block, not the typed reading of it, and not an allowlist of the fields this package
happens to know. The same for the identifiers configuration filed the call under, which
join the `call` block — passed through only when they arrived, never derived. This is the
one thing the envelope carries that is neither ours nor LiveKit's, and it is carried for
the same reason `livekit` is nested verbatim: a field the other side adds tomorrow reaches
it back without a release here.

`livekit.session_report` is `ctx.make_session_report().to_dict()` unmodified — chat history
with timestamps, per-provider usage, recorded events, session options, SDK version. The key
is present on every `call.ended`, null when the report could not be built, so a consumer
never has to handle two shapes of the same event. It is absent from `call.started`.

`tags` carries `ctx.tagger` outcome and tags.

Delivery: `POST`, HMAC signature over timestamp and body when a secret is configured, an
idempotency key per event, retries on 5xx and network errors with backoff, fail-fast on 4xx.

### Outcome

`call.status` is this package's vocabulary, mapped from what LiveKit reports, never passed
through from a carrier. A consumer must not have to change because a trunk moved from
Twilio to Telnyx.

LiveKit's `sip.callStatus` is itself normalized — five strings defined in one function in
`livekit/sip` (`pkg/sip/participant.go`), unchanged since November 2024, with carrier
specifics confined to separate key namespaces (`sip.twilio.*`, `sip.telnyx.*`). What it is
not is complete: busy, declined, unavailable and no-answer are all written as *no
attribute*, so it freezes at `ringing` and the participant disappears. That was deliberate
on LiveKit's part.

So the outcome is read from the participant's disconnect reason:

| disconnect reason | `call.status` |
| --- | --- |
| answered at any point | `completed` |
| `USER_UNAVAILABLE`, `CONNECTION_TIMEOUT`, unknown | `no_answer` |
| `USER_REJECTED` | `rejected` |
| `CLIENT_INITIATED` before an answer | `canceled` |
| `SIP_TRUNK_FAILURE`, `MEDIA_FAILURE`, `AGENT_ERROR` | `failed` |

`USER_REJECTED` covers busy, declined, auth failure and carrier block alike, so busy and
declined are not distinguishable from inside the room and this package does not pretend
otherwise. The exact SIP code exists only in the error returned to whoever called
`CreateSIPParticipant` with `wait_until_answered`, which is the platform, not the agent.

## 9. Recording

The SDK records locally to OGG/Opus, stereo, via PyAV, then reads the whole file into memory
and sends it to LiveKit Cloud in a single multipart POST with no chunking and no size guard
(`telemetry/traces.py:406`). Roughly 700 KB per minute at the default bitrate.

This package:

- **Default: object storage.** One S3-compatible client, configurable endpoint, so S3 and
  R2 are the same path. Recording and transcript are written under the same `call_id`.
  Requires the `s3` extra.
- **Fallback: multipart to the webhook endpoint**, for zero-configuration use. No size
  ceiling, matching the SDK's own behaviour.
- The webhook is posted before the upload, so the call is closed out with a terminal status
  even if the process dies mid-upload.
- **Keys travel, not URLs.** A payload never carries a link that plays the recording to
  whoever holds it, and never the bucket, which is deployment configuration a consumer has
  no business acting on. Whoever holds the credentials reads the object.
- Because the webhook goes first, `recording.delivery: "storage"` in `call.ended` states an
  intent. `call.recording` follows a successful upload with the same keys and
  `"stored": true`, which states the fact; a failed upload sends nothing.

## 10. Logging

Module-named loggers obtained from `logging.getLogger`. The package never sets a level,
never attaches a handler and never configures the root logger. Whatever the host has
configured is what applies.

## 11. Constraints worth knowing

- **Metadata limits** are server config, not constants: 512 KiB for metadata, 64 KiB for
  attributes summed across keys and values. Servers older than 2026-06-17 cap both at
  64000 bytes. SIP dispatch rules are not size-checked at all — an oversized rule saves
  silently and fails later when the participant joins.
- **`room_config`** on a SIP dispatch rule applies only when the SIP participant creates the
  room. Against an existing room it is silently ignored. Use a unique room per call or an
  explicit `CreateAgentDispatch`.
- **`AgentServer.update_options()`** declares `shutdown_process_timeout` and
  `session_end_timeout` with plain defaults instead of sentinels while testing them with
  `is_given`, so any call to it silently resets both to 10s and 300s.
- **`RoomAgentDispatch.attributes`** exists in the protocol on main but not in 1.1.7. Only
  `metadata` is safe to rely on.
- **`core.state` must stay bound to the submodule.** Re-exporting `state()` from
  `core/__init__.py` under that name shadows it, and every internal
  `from ..core import state` then binds a function instead — a failure that only surfaces
  at call time. The public alias is `core.call_state`.
- **An inbound call's participant is already in the room** when the job starts, and the SDK
  replays already-present participants to participant entrypoints *inside* `ctx.connect()`.
  Registering an entrypoint afterwards never sees them, so `attach()` checks
  `room.remote_participants` itself.
- **An outbound SIP participant exists from the first ring**, carrying
  `sip.callStatus` of `dialing` or `ringing`. Reporting that as the call starting would
  claim somebody answered while the phone is still ringing, so the start is held until the
  status reaches `active`. Inbound is already answered when the participant appears, so the
  same check passes it through and nothing has to declare a direction.
- **`rtc.Room.sid` is an async property.** Reading it from synchronous code yields a
  coroutine that is never awaited. The job's copy of the room carries the same value as a
  plain string.
- **Shutdown callbacks are gathered concurrently**, not run in registration order. Nothing
  may depend on one running before another.
- **A simulated job — console mode — has a mock room nobody joins**, so the participant
  entrypoint never fires. `attach()` detects it through `ctx.is_fake_job()` and reports the
  call as started when the session starts instead. Such a call has no parties and no SIP
  envelope.

## 12. Versioning

Semantic versioning, not CalVer. A date says when a release happened; it says nothing about
whether a receiver written against the last one still parses this one, and that question is
the entire product here. LiveKit itself is on SemVer, and this package declares a range
against it, so the schemes match.

The bump follows a rule rather than taste: additive payload fields and fixes are patches,
anything a receiver could choke on is a minor. In `0.x` the minor is the breaking position —
`^0.1.0` admits `0.1.x` and not `0.2.0` — so `0.2.0` is not a big release, it is one the
consumers need to hear about.

## 13. Later

- Prompting a caller who has gone quiet before hanging up on them, which needs a way to
  make the agent speak that does not assume a speech stack.
- Transfer.
- Splitting a module into its own distribution, if dependencies diverge. Import paths are
  already shaped for it.
