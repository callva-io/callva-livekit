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
from callva.livekit import config as callva_config
from callva.livekit import webhook as callva_webhook

server = AgentServer()

@server.rtc_session(agent_name="my-agent", on_session_end=callva_webhook.on_session_end)
async def entrypoint(ctx):
    await ctx.connect()

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

`call.started` when someone is on the other end, `call.ended` when it is over.

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
    "status": "completed"
  },
  "livekit": {
    "room": { "name": "call-1", "sid": "RM_…", "metadata": null },
    "job":  { "id": "AJ_…", "dispatch_id": "AD_…", "agent_name": "my-agent", "…": "…" },
    "participant": { "identity": "sip_…", "attributes": { "sip.callID": "…" } },
    "sip":  { "callID": "…", "phoneNumber": "…", "twilio": { "callSid": "…" } },
    "session_report": { "chat_history": {}, "usage": [], "options": {}, "…": "…" }
  },
  "recording": { "url": "https://cdn.example/8f1c….ogg" },
  "tags": { "tags": ["lk.success"], "outcome": "success", "reason": null }
}
```

Everything LiveKit produces is nested under `livekit` **verbatim** — including
`session_report`, which is `ctx.make_session_report().to_dict()` untouched: full chat
history with timestamps, per-provider token usage, recorded events, session options. A
field the SDK adds tomorrow reaches you without a release here.

The `call` block is the only thing reshaped, because it is the only thing LiveKit does not
model: a stable id across both events, a direction, and a `from` and a `to`.

Requests carry `X-Webhook-Idempotency-Key`, and `X-Webhook-Signature` when a secret is set —
`sha256` HMAC over `{timestamp}.{body}`, with `X-Webhook-Timestamp` alongside. Delivery
retries on 5xx and network errors and fails fast on 4xx.

## Configuration for a call

Configuration reaches the agent through **agent dispatch metadata**, read as
`ctx.job.metadata`:

```json
{ "callva": { "call_id": "…", "direction": "outbound", "config": { "prompt": "…" } } }
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
  "prompt":    "You are speaking with {{ name }}.",
  "greeting":  "Hi {{ name }}, how can I help?",
  "variables": { "name": "Anna", "attempt": 2, "vip": true },
  "webhook":   { "url": "https://tenant.example/hook", "secret": "…" },
  "extra":     { "anything": "you like" }
}
```

`{{ name }}` is substituted into both `prompt` and `greeting`. A placeholder with no
variable is left exactly as it was and logged — one missing key must not take down a call
that is already ringing. JSON types survive: `config.variables.get_int("attempt")` is `2`.
`extra` is never interpreted.

`webhook` in the response overrides the environment, which is what lets one worker serve
many tenants.

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
| `RECORDING_S3_PUBLIC_BASE_URL` | Turns the object key into the URL sent in the webhook |
| `RECORDING_S3_PREFIX` | Key prefix inside the bucket |

Every value has a constructor argument that takes precedence.

## Recording

With a bucket configured, the recording and the session report are written under the same
call id — `<call_id>.ogg` and `<call_id>.json` — and the webhook carries the URL, which is
known before the bytes move. Needs `record=True` on `session.start()` and the codecs extra
(`pip install "livekit-agents[codecs]"`).

Without a bucket, and only if a webhook target is set, the recording follows the webhook as
a multipart `call.recording` request. Convenient for getting started; object storage is the
answer for long calls.

The `call.ended` webhook is always sent **before** the upload, so a call is closed out with
a terminal status even if the process does not survive the transfer.

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
