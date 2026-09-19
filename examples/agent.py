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

from callva.livekit import call as callva_call
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

    # The default: a prompt that cannot be resolved ends the call, because an agent
    # without one is a broken call either way. With no configuration source set at all
    # nothing fails — there is simply nothing to resolve — so this example still runs.
    #
    # A refusal is the other outcome, and the only one with something to say first.
    try:
        config = await callva_config.load()
    except callva_config.ConfigRefused as refused:
        await _refuse(ctx, refused)
        return
    except callva_config.ConfigError:
        logging.warning("no configuration for this call; it has already been ended")
        return

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


async def _refuse(ctx: JobContext, refused: callva_config.ConfigRefused) -> None:
    """The endpoint said this call must not go ahead. Say so, then hang up.

    The message is the responder's, not ours, and saying it is the only reason the
    refusal travels as an object. What the code means is the responder's business too:
    it is logged and never matched on.
    """
    logging.info("refused (%s): %s", refused.reason_code, refused.error)

    if refused.caller_message:
        session = AgentSession(tts="cartesia/sonic-2")
        await session.start(agent=Agent(instructions=""), room=ctx.room)
        await session.say(refused.caller_message)

    await callva_call.end(ctx, reason=refused.reason_code or "the call was refused")


if __name__ == "__main__":
    cli.run_app(server)
