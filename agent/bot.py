"""Agent entry point — one bot, every transport.

Pipecat's runner serves a FastAPI app that handles BOTH:
  - Browser WebRTC with a prebuilt UI  (local dev on your Mac, $0)
  - Twilio Media Streams WebSocket     (real phone calls, Phase 1)

Local dev:
    python -m agent.bot                    # then open http://localhost:7860
Phone (after `ngrok http 7860` and pointing your Twilio number's webhook
at the printed URL — see README):
    python -m agent.bot --transport twilio

Tenant selection: BUSINESS_SLUG env var (single-tenant per process in v1;
multi-tenant routing by called-number comes with the Phase 2 dispatcher).
"""
import os

from loguru import logger
from pipecat.pipeline.runner import PipelineRunner
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport, parse_telephony_websocket
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams

from . import monitor  # noqa: F401 — registers /monitor routes + loguru sink on the runner app
from . import warmup  # noqa: F401 — pre-warms Ollama + Whisper-MLX on app startup
from .pipeline import build_call_task, transport_audio_params

BUSINESS_SLUG = os.getenv("BUSINESS_SLUG", "luigis-carlton")


def _twilio_params() -> FastAPIWebsocketParams:
    # Serializer is attached per-call in bot() (needs stream_sid); params here
    # cover audio + turn-taking. Twilio streams 8kHz mulaw; serializer resamples.
    return FastAPIWebsocketParams(**transport_audio_params())


transport_params = {
    "webrtc": lambda: TransportParams(**transport_audio_params()),
    "twilio": _twilio_params,
}


async def bot(runner_args: RunnerArguments):
    call_id = None

    # For telephony, pull the call/stream ids and attach the Twilio serializer
    # (enables proper mulaw framing + auto hang-up on EndFrame).
    if type(runner_args).__name__ == "WebSocketRunnerArguments":
        transport_type, call_data = await parse_telephony_websocket(runner_args.websocket)
        logger.info(f"telephony call: {transport_type} {call_data}")
        call_id = call_data.get("call_id") or call_data.get("call_sid")

        serializer = TwilioFrameSerializer(
            stream_sid=call_data["stream_id"],
            call_sid=call_data.get("call_id"),
            account_sid=os.getenv("TWILIO_ACCOUNT_SID"),
            auth_token=os.getenv("TWILIO_AUTH_TOKEN"),
        )
        params = FastAPIWebsocketParams(**transport_audio_params(), serializer=serializer)
        from pipecat.transports.websocket.fastapi import FastAPIWebsocketTransport

        transport = FastAPIWebsocketTransport(websocket=runner_args.websocket, params=params)
    else:
        transport = await create_transport(runner_args, transport_params)

    task = await build_call_task(transport, slug=BUSINESS_SLUG, call_id=call_id)
    runner = PipelineRunner(handle_sigint=getattr(runner_args, "handle_sigint", True))
    await runner.run(task)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
