# callva-livekit

Drop-in call webhooks and per-call configuration for any LiveKit agent.

Two things most voice agents need and the LiveKit SDK does not provide: a webhook when a
call starts and ends, and a way to get the prompt for *this* call from somewhere other than
your source code.

Both are opt-in. Installing the package activates nothing, patches nothing, and changes
nothing about how your session behaves.

## Install

```bash
pip install callva-livekit          # webhooks + config
pip install callva-livekit[s3]      # + recording upload to S3 or R2
```

## Use

```python
from livekit.agents import Agent, AgentServer, AgentSession
from callva.livekit import call as callva_call
from callva.livekit import config as callva_config
from callva.livekit import webhook as callva_webhook

server = AgentServer()

@server.rtc_session(agent_name="my-agent", on_session_end=callva_webhook.on_session_end)
async def entrypoint(ctx):
    await ctx.connect()

    if await callva_call.await_pickup(ctx) is None:      # outbound: rang, nobody answered
        await callva_call.end(ctx, wait=False)
        return

    config = await callva_config.load()

    session = AgentSession(...)
    callva_webhook.attach(session)

    await session.start(agent=Agent(instructions=config.prompt), room=ctx.room, record=True)
    await session.generate_reply(instructions=config.greeting)
```

Neither call takes a `JobContext` — it comes from the SDK's own contextvar. `load()` hands
you an object; what you do with the prompt is your decision.

A complete runnable agent is in [examples/agent.py](examples/agent.py), with a local
receiver that prints what arrives in [examples/receiver.py](examples/receiver.py).

## What arrives

`call.dialing` when the dial goes out, `call.started` when someone is on the other end,
`call.ended` when it is over — always, however it ended.

| event | when | `call.status` |
| --- | --- | --- |
| `call.dialing` | the dial went out, the phone is ringing | `dialing` |
| `call.started` | somebody answered | `in_progress` |
| `call.ended` | terminal, always sent | `completed` · `no_answer` · `rejected` · `canceled` · `failed` |

There is one terminal event, not two: how a call ended is a value, not a kind, so a
consumer has exactly one thing to handle.

The vocabulary is this package's own. LiveKit's `sip.callStatus` — `dialing`, `ringing`,
`automation`, `active`, `hangup` — is normalized by LiveKit itself and does not change when
a trunk moves between carriers, but it carries no terminal outcome at all: a refused call
simply stops updating and the participant vanishes. The outcome therefore comes from the
participant's disconnect reason, which cannot tell busy from declined — so this package
does not claim to either. `rejected` means one of them. The raw signals travel untouched in
`livekit.sip.callStatus` and `livekit.disconnect_reason`.

```json
{
  "event": "call.ended",
  "id": "8f1c…:call.ended:1757...",
  "timestamp": 1757600000.12,
  "call": {
    "id": "8f1c…",
    "direction": "inbound",
    "from": { "number": "+37255512345", "identity": "sip_+37255512345", "name": null },
    "to":   { "number": "+3726001234", "identity": null, "name": null },
    "started_at": 1757599940.5, "ended_at": 1757600000.1, "duration": 59.6,
    "status": "completed",
    "project_id": "pr_…", "tenant_id": "tn_…", "type": "outbound_campaign"
  },
  "agent": { "id": "ag_…", "name": "Anna", "…": "the agent block exactly as configured" },
  "environment": "production",
  "livekit": {
    "room": { "name": "call-1", "sid": "RM_…", "metadata": null },
    "job":  { "id": "AJ_…", "dispatch_id": "AD_…", "agent_name": "my-agent", "…": "…" },
    "participant": { "identity": "sip_…", "attributes": { "sip.callID": "…" } },
    "sip":  { "callID": "…", "phoneNumber": "…", "twilio": { "callSid": "…" } },
    "session_report": { "chat_history": {}, "usage": [], "options": {}, "…": "…" }
  },
  "recording": { "delivery": "storage", "audio_key": "8f1c….ogg",
                 "session_report_key": "8f1c….session.json" },
  "tags": { "tags": ["lk.success"], "outcome": "success", "reason": null }
}
```

Everything LiveKit produces is nested under `livekit` **verbatim** — including
`session_report`, which is `ctx.make_session_report().to_dict()` untouched: full chat
history with timestamps, per-provider token usage, recorded events, session options. A
field the SDK adds tomorrow reaches you without a release here.

The `call` block is the only thing reshaped, because it is the only thing LiveKit does not
model: a stable id across both events, a direction, and a `from` and a `to`. Whatever the
configuration source filed the call under — `project_id`, `tenant_id`, `type` — travels
back in it untouched, and a key that never arrived is absent rather than null.

`agent` is the configuration response's own `agent` block, echoed back on every event
exactly as it arrived, including whatever a per-call override changed and whatever this
schema does not name. Nothing in it is interpreted here; the sender reads its own values
back, which is what a platform deciding per call needs. `environment` is the same
passthrough for the response's top-level `environment`, and is null when the source named
none — it is never read from this process's environment.

Requests carry `X-Webhook-Idempotency-Key`, and `X-Webhook-Signature` when a secret is set —
`sha256` HMAC over `{timestamp}.{body}`, with `X-Webhook-Timestamp` alongside. Delivery
retries on 5xx and network errors and fails fast on 4xx.

## Being answered, and hanging up

```python
participant = await callva_call.await_pickup(ctx)   # None if nobody picked up
await callva_call.end(reason="the agent said goodbye")
callva_call.leave_console_when_done(ctx)            # console only; a real worker stays up
```

An **inbound** call is answered by the time a participant exists. An **outbound** one is
not: the participant appears while the phone is still ringing, and `sip.callStatus` is
what says otherwise. Waiting for a participant alone reports a ringing call as live, and
reports one that was never picked up as live too. `await_pickup` waits for the real thing,
and a trunk that answers and hangs up inside a second does not slip past it.

Nothing is reported from there and nothing is torn down — the reason is left on the call's
state, where `webhook` turns it into an outcome, and hanging up stays your decision.

`end` releases the caller before it ends the job. Shutting the job down only takes the
agent out of the room; whoever is on the other end stays connected to a room with nobody
in it until the server's `empty_timeout` expires, which on a telephone call means it has
not ended. It waits for the agent to stop speaking first — pass `wait=False` for a call
being abandoned rather than finished — and the report and the recording still go out,
because they belong to the shutdown sequence and a closed room does not interrupt it.

## When a call goes wrong

```python
callva_webhook.collect_errors()     # once, where the worker starts up
```

Every error logged anywhere in the process is kept and delivered inside `call.ended`, as
`errors`, with the traceback where there was one. There is no separate event for a call
that fell apart: that call still ends and still reports — what was missing was ever saying
why.

It attaches a handler to the root logger, which is a process-wide thing to do and so is
asked for rather than assumed. Our own errors are never collected, because a failing
delivery would report itself forever.

## Configuration for a call

Configuration reaches the agent through **agent dispatch metadata**, read as
`ctx.job.metadata`:

```json
{ "callva": { "call_id": "…", "direction": "outbound", "to": "+372…",
              "config": { "agent": { "prompt": "…" } } } }
```

Put a `config_url` there instead of a `config`, or set `CONFIG_URL`, and the agent
follows that instead. The request is the question — it carries who is calling, which number
they reached and the whole SIP envelope — so the endpoint can answer "this number belongs to
that customer, here is their prompt". That is the inbound case in one hop.

A pointer can also be a local file — `file:///etc/agent.json` or a plain `./agent.json`. It
is read as it is, with no request and no waiting for anyone to join, which makes it the
shortest development loop there is. It cannot answer per caller, so it is not the production
channel.

The response:

```json
{
  "call":  { "id": "019f0c4e-1f3a-7a55-9d21-2b0e5f77a1c3", "direction": "inbound",
             "project_id": "pr_…", "tenant_id": "tn_…", "type": "outbound_campaign" },
  "environment": "production",
  "agent": {
    "id": "…", "name": "Anna",
    "prompt":   "You are speaking with {{ name }}.",
    "greeting": "Hi {{ name }}, how can I help?",
    "greeting_type": "message",
    "agent_waits_for_user": false,
    "prompt_variables": { "name": "Anna", "attempt": 2, "vip": true },
    "max_duration_seconds": 600,
    "user_silence_timeout_seconds": 15
  },
  "preset":   { "name": "gemini_vertex", "config": { "voice": "Aoede" } },
  "tools":    { "endCall": { "type": "end_call", "enabled": true } },
  "services": { "webhook": { "url": "https://tenant.example/hook", "secret": "…" } }
}
```

Every value has one home. The prompt, the greeting and the variables belong to the agent;
the id belongs to the call; the webhook belongs to the services. Nothing is repeated at
the root for convenience, so nothing can disagree with itself.

`preset` is the only block that varies with the speech stack. An agent that builds its own
pipeline ignores it and reads the rest; an agent that is assembled from configuration reads
all of it. `tools` describes what the agent may call, not what it did.

`{{ name }}` is substituted into both `prompt` and `greeting`. A placeholder with no
variable is left exactly as it was and logged — one missing key must not take down a call
that is already ringing. JSON types survive: `config.variables.get_int("attempt")` is `2`.

Opening the call is three states, not two, and `config.agent` carries all three:
`speaks_first` says whether the agent opens at all, and `greeting_type` says whether the
greeting is a line to speak (`message`) or an instruction to compose one from (`prompt`).

`services.webhook` overrides the environment, which is what lets one worker serve many
tenants.

`call.id` files the call under an id you already hold. A responder that creates a record
for the call before answering can name it here, and every event afterwards carries that id
— so both sides address one record, and neither has to store a field holding the other's
identifier. It is the only part of the call's identity configuration may decide. A
dispatcher that named the call outranks it, and an id offered after the first event has
gone out is refused with a warning.

`extra` is there for what this schema does not describe, and is never interpreted.

What the responder sends about itself comes back in the report: the whole `agent` block,
the `environment`, and the `project_id`, `tenant_id` and `type` it filed the call under.
A responder that decides something per call therefore reads back the value that was in
force for that call, not the one it has stored.

When configuration cannot be resolved the call is **terminated** and the reason logged. An
agent without its prompt is a broken call either way. Pass `on_error="continue"` if you
would rather carry on.

Room metadata is deliberately not used as a channel: it is broadcast to every participant
in the room, so a prompt placed there is readable by any connected client.

## Environment

| Variable | Purpose |
| --- | --- |
| `WEBHOOK_URL` | Where call events are sent |
| `WEBHOOK_SECRET` | HMAC signing secret |
| `WEBHOOK_TIMEOUT` | Per-attempt timeout, seconds (default 30) |
| `CONFIG_URL` | Endpoint asked for per-call configuration |
| `CONFIG_API_KEY` | Sent to it as a bearer token |
| `CONFIG_TIMEOUT` | Per-attempt timeout, seconds (default 10) |
| `CALL_DIRECTION` | Default direction when nothing declares one |
| `RECORDING_S3_BUCKET` | Enables recording upload |
| `RECORDING_S3_ENDPOINT_URL` | Set this for R2 or any S3-compatible store |
| `RECORDING_S3_REGION`, `RECORDING_S3_ACCESS_KEY_ID`, `RECORDING_S3_SECRET_ACCESS_KEY` | Credentials |
| `RECORDING_S3_PREFIX` | Key prefix inside the bucket |

Every value has a constructor argument that takes precedence.

## Recording

With a bucket configured, the recording and the session report are written under the same
call id — `<call_id>.ogg` and `<call_id>.session.json` — and `call.ended` carries
`recording.delivery: "storage"` with `recording.audio_key` and
`recording.session_report_key`, which are known before the bytes move. The session report
is not a transcript, and it deliberately does not take the plain `<call_id>.json` name: a
platform that stores a transcript of its own is likely to have claimed it, and this would
land on top of it. Needs `record=True` on `session.start()` and the codecs extra
(`pip install "livekit-agents[codecs]"`).

What travels is the key, never a URL to fetch it with. A link that plays a recording to
whoever holds it does not belong in a webhook body, and the bucket is this deployment's own
configuration rather than something a consumer should act on. Whoever holds the credentials
reads the object.

Without a bucket, and only if a webhook target is set, the recording follows the webhook as
a multipart `call.recording` request. Convenient for getting started; object storage is the
answer for long calls.

The `call.ended` webhook is always sent **before** the upload, so a call is closed out with
a terminal status even if the process does not survive the transfer. That makes
`delivery: "storage"` a statement of intent, so a `call.recording` event follows a
successful upload carrying the same keys plus `"stored": true` — the statement of fact. A
failed upload sends nothing, and the call is still closed out.

## Versioning

Semantic versioning, and the compatibility promise is about **what a receiver has to
parse**, not about the size of the diff:

- **0.1.x** — fixes, and fields *added* to a payload. Adding is not breaking: a receiver
  ignores keys it does not know, and because everything LiveKit produces is nested verbatim,
  fields the SDK adds arrive without a release here at all.
- **0.2.0** — anything a receiver could choke on: a field renamed or removed, an event name
  changed, a header changed, an environment variable renamed, a public function changed.
- **1.0.0** — when the contract is worth freezing.

In `0.x` the digits are shifted one place: the middle number is the breaking one, which is
what `^0.1.0` means to every resolver. So a `0.2.0` here is not a large release — it is a
release that someone's receiver has to be told about.

## Two things worth knowing

**Your agent needs `agent_name` set and explicit dispatch.** With automatic dispatch
`job.metadata` arrives empty, and nothing can be addressed to this call.

**If you cannot use `on_session_end`** — you are still on `WorkerOptions` — `attach()` falls
back to a shutdown callback and says so in the log. That path is bounded by
`shutdown_process_timeout`, 10 seconds by default, after which the worker kills the
process mid-upload. Raise it, or move to `AgentServer`.

## License

Apache-2.0
