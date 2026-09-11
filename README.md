# callva-livekit-tools

Drop-in call webhooks and per-call configuration for any LiveKit agent.

Two things your agent probably needs and the LiveKit SDK does not provide: a webhook when a
call starts and ends, and a way to get the prompt for *this* call from somewhere other than
your source code.

Both are opt-in. Installing the package activates nothing.

> **Status: in development.** The contracts are settled and written down in
> [DESIGN.md](DESIGN.md); the implementation is not finished yet.

## Install

```bash
pip install callva-livekit-tools          # webhooks + config
pip install callva-livekit-tools[s3]      # + recording upload to S3 or R2
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

    cfg = await callva_config.load()

    session = AgentSession(...)
    callva_webhook.attach(session)

    await session.start(agent=Agent(instructions=cfg.prompt), room=ctx.room)
    await session.generate_reply(instructions=cfg.greeting)
```

Nothing is patched and nothing is substituted behind your back. `load()` hands you an
object; what you do with the prompt is your call.

## What you get

`call.started` when the call goes live, `call.ended` when it finishes. The end event carries
the LiveKit session report verbatim — full chat history with timestamps, per-provider token
usage, session options, the recording — inside a thin envelope that adds the one thing
LiveKit does not model: a normalized call with a direction and a `from` and a `to`.

Per-call configuration arrives through agent dispatch metadata, or is fetched from an
endpoint you point the agent at. The request carries the call context, so the endpoint can
answer "who is calling which number" and pick the prompt accordingly.

## Configure

| Variable | Purpose |
| --- | --- |
| `CALLVA_WEBHOOK_URL` | Where call events are sent |
| `CALLVA_WEBHOOK_SECRET` | HMAC signing secret |
| `CALLVA_CONFIG_URL` | Endpoint asked for per-call configuration |
| `CALLVA_DIRECTION` | Default call direction when nothing declares one |

Every variable has a constructor argument that takes precedence, and a per-call resolver for
multi-tenant workers.

## Notes

Your agent needs `agent_name` set and explicit dispatch for `job.metadata` to arrive.

If you cannot use `on_session_end` — you are still on `WorkerOptions` — `attach()` falls back
to a shutdown callback, which is bounded by `shutdown_process_timeout` (10 seconds by
default, after which the worker kills the process). Raise it, or move to `AgentServer`.

## License

Apache-2.0
