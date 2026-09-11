"""A complete LiveKit agent wired to CallVA, and nothing else.

Run the receiver in one terminal and this in another:

    python examples/receiver.py
    WEBHOOK_URL=http://localhost:878/hook \\
    WEBHOOK_SECRET=dev-secret \\
        python examples/agent.py dev

Needs ``LIVEKIT_URL``, ``LIVEKIT_API_KEY`` and ``LIVEKIT_API_SECRET``. The speech stack
is named by string, so it is served by LiveKit Inference and needs no other credentials.
Recording needs the codecs extra: ``pip install "livekit-agents[codecs]"``.
"""

from __future__ import annotations

import logging

from livekit.agents import Agent, AgentServer, AgentSession, JobContext, cli

from callva.livekit import config as callva_config
from callva.livekit import webhook as callva_webhook

logging.basicConfig(level=logging.INFO)

FALLBACK_PROMPT = (
    "You are a friendly voice assistant on a phone call. Keep answers to one or two "
    "sentences. Never read out punctuation or formatting."
)

server = AgentServer()


@server.rtc_session(
    agent_name="callva-example",
    on_session_end=callva_webhook.on_session_end,
)
async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()

    # `continue` keeps this example runnable with no configuration source at all. A real
    # agent leaves the default, which ends the call when its prompt cannot be resolved.
    config = await callva_config.load(on_error="continue")

    session = AgentSession(
        stt="deepgram/nova-3",
        llm="openai/gpt-4.1-mini",
        tts="cartesia/sonic-2",
    )

    callva_webhook.attach(session)

    await session.start(
        agent=Agent(instructions=config.prompt or FALLBACK_PROMPT),
        room=ctx.room,
        record=True,
    )

    await session.generate_reply(
        instructions=config.greeting or "Greet the caller and ask how you can help."
    )


if __name__ == "__main__":
    cli.run_app(server)
