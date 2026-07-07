"""Pipeline factory — the heart of the media plane.

One function builds the call pipeline for ANY transport (local WebRTC dev,
Twilio Media Streams, later LiveKit/SIP), with services chosen by env:

  STACK=local   -> Whisper-MLX STT + Ollama LLM + Kokoro TTS   ($0, on your M5 Pro)
  STACK=hosted  -> Deepgram STT + OpenAI/Anthropic LLM + Cartesia TTS (premium tier)

Turn-taking is the 3-layer stack from the platform design:
  Silero VAD (acoustic) + Smart Turn v3 (semantic) + Pipecat endpointing.
Interruptions/barge-in are handled by the framework (allow_interruptions=True).

NOTE (Pipecat 1.5): VAD/turn-analyzer are NOT transport params in this
version — TransportParams has no vad_analyzer/turn_analyzer fields, so
setting them there is silently ignored (no VAD frames -> STT never
segments audio -> speech is never transcribed, even though typed text
still works). They're wired on the LLM user aggregator instead, via
LLMUserAggregatorParams below.
"""
import os
import uuid
from datetime import datetime

import httpx
from loguru import logger

from pipecat.frames.frames import TTSSpeakFrame
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)

from .monitor import MonitorObserver
from .prompts import build_system_prompt
from .tools import make_handlers, tools

CONTROL_PLANE = os.getenv("CONTROL_PLANE_URL", "http://127.0.0.1:8080")
STACK = os.getenv("STACK", "local")
# Language: "en" | "es" | "multi" (bilingual auto-detect — Deepgram detects
# English vs Spanish per utterance, incl. code-switching; the LLM replies in
# whichever the caller used). "mainly Spanish" → use "multi".
LANGUAGE = os.getenv("LANGUAGE", "en").lower()
# Deepgram language code and Cartesia synthesis language, derived from LANGUAGE.
_DG_LANG = {"en": "en-US", "es": "es", "multi": "multi"}.get(LANGUAGE, "en-US")
# Semantic endpointing. "local" = Smart Turn v3 (best perceived latency, but
# pulls torch + a model download — heavy for a cloud image). "off" = rely on
# Silero VAD + the STT provider's endpointing (Deepgram has its own), which
# keeps the hosted image lean and cold-starts fast. Silero VAD is ALWAYS on.
SMART_TURN = os.getenv("SMART_TURN", "local").lower()


def build_services():
    """Swappable model layer — the two-tier strategy in code."""
    if STACK == "hosted":
        from pipecat.services.cartesia.tts import CartesiaTTSService, GenerationConfig
        from pipecat.services.deepgram.stt import DeepgramSTTService
        from pipecat.services.openai.llm import OpenAILLMService
        from pipecat.transcriptions.language import Language

        stt = DeepgramSTTService(
            api_key=os.getenv("DEEPGRAM_API_KEY"),
            # numerals: "one two three" -> "123" — essential for phone numbers,
            # party sizes, times. smart_format also tidies dates/numbers.
            # language: "multi" auto-detects English/Spanish (nova-3), so one
            # number serves both; "es" locks Spanish, "en-US" locks English.
            settings=DeepgramSTTService.Settings(
                numerals=True,
                smart_format=True,
                language=_DG_LANG,
            ),
        )
        llm = OpenAILLMService(model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"))
        # Cartesia synthesis language. Bilingual ("multi") defaults to English;
        # sonic-3.5 is multilingual so Spanish replies still render acceptably
        # through the same voice (swap to a Spanish voice if you want perfect
        # Spanish pronunciation).
        _tts_lang = {"en": Language.EN, "es": Language.ES, "multi": Language.EN}.get(
            LANGUAGE, Language.EN
        )
        tts = CartesiaTTSService(
            api_key=os.getenv("CARTESIA_API_KEY"),
            voice_id=os.getenv("CARTESIA_VOICE_ID", ""),
            # speed 0.95 = a touch slower than default → warmer, clearer on a
            # phone line. Range 0.6–1.5. To change the voice itself, swap
            # CARTESIA_VOICE_ID (audition warm voices at play.cartesia.ai).
            params=CartesiaTTSService.InputParams(
                language=_tts_lang,
                generation_config=GenerationConfig(speed=0.95),
            ),
        )

        # Provider fallback: if Cartesia errors mid-call, fail over to Deepgram
        # Aura TTS (reuses the Deepgram key you already have — no new provider).
        # Off by default; enable with TTS_FALLBACK=on.
        if os.getenv("TTS_FALLBACK", "off").lower() != "off":
            from pipecat.services.deepgram.tts import DeepgramTTSService
            from pipecat.pipeline.service_switcher import (
                ServiceSwitcher,
                ServiceSwitcherStrategyFailover,
            )

            backup_tts = DeepgramTTSService(
                api_key=os.getenv("DEEPGRAM_API_KEY"),
                voice=os.getenv("DEEPGRAM_TTS_VOICE", "aura-2-thalia-en"),
            )
            tts = ServiceSwitcher(
                services=[tts, backup_tts],
                strategy_type=ServiceSwitcherStrategyFailover,
            )
            logger.info("TTS provider fallback ON: Cartesia → Deepgram Aura")
    else:
        # Fully local, $0/minute. Requires: ollama serve + `ollama pull qwen2.5:14b`
        from pipecat.services.kokoro.tts import KokoroTTSService
        from pipecat.services.ollama.llm import OLLamaLLMService
        from pipecat.services.whisper.stt import WhisperSTTServiceMLX, MLXModel

        stt = WhisperSTTServiceMLX(model=MLXModel.LARGE_V3_TURBO_Q4)
        llm = OLLamaLLMService(
            model=os.getenv("OLLAMA_MODEL", "qwen2.5:14b"),
            base_url=os.getenv("OLLAMA_URL", "http://localhost:11434/v1"),
        )
        tts = KokoroTTSService(voice_id=os.getenv("KOKORO_VOICE", "af_heart"))
    return stt, llm, tts


def transport_audio_params() -> dict:
    """Audio config shared by every transport.

    VAD + turn detection are NOT configured here (see module note above) —
    they're attached to the user context aggregator in build_call_task().
    """
    return dict(
        audio_in_enabled=True,
        audio_out_enabled=True,
    )


async def fetch_business_context(slug: str) -> dict:
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(f"{CONTROL_PLANE}/agent/{slug}/context")
        r.raise_for_status()
        return r.json()


async def build_call_task(transport, slug: str, call_id: str | None = None) -> PipelineTask:
    """Assemble the per-call pipeline: one call = one task = one worker."""
    call_id = call_id or str(uuid.uuid4())
    ctx = await fetch_business_context(slug)

    stt, llm, tts = build_services()

    # Register tenant-scoped tool handlers
    handlers = make_handlers(slug, call_id)
    for name, fn in handlers.items():
        if not name.startswith("_"):
            llm.register_function(name, fn)

    system_prompt = build_system_prompt(ctx, datetime.now(), language=LANGUAGE)
    context = LLMContext(
        messages=[{"role": "system", "content": system_prompt}],
        tools=tools,
    )
    # VAD (acoustic) + optional Smart Turn v3 (semantic) live on the user
    # aggregator in Pipecat 1.5. The VAD analyzer here is what actually emits
    # VADUserStartedSpeakingFrame/VADUserStoppedSpeakingFrame — without it,
    # the STT service never segments audio and speech is never transcribed.
    # stop_secs 0.4 (default 0.2 is aggressive → it clips callers mid-sentence).
    # A touch more grace before deciding they're done makes turn-taking feel
    # natural without adding much latency.
    user_params = LLMUserAggregatorParams(
        vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.4)),
    )
    if SMART_TURN != "off":
        # Imported lazily so the hosted image can skip torch + the model download.
        from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
        from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
        from pipecat.turns.user_turn_strategies import UserTurnStrategies

        user_params.user_turn_strategies = UserTurnStrategies(
            stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3())],
        )
    else:
        logger.info("Smart Turn disabled (SMART_TURN=off) — using VAD + STT endpointing")

    aggregators = LLMContextAggregatorPair(context, user_params=user_params)

    # Optional naturalness processors sit between the LLM and TTS so they can
    # inject short spoken frames. Both default OFF (env flags) and only ADD
    # audio — they never drop or change the real conversation.
    extras = []
    if os.getenv("BACKCHANNEL", "off").lower() != "off":
        from .naturalness import Backchannel
        extras.append(Backchannel())
        logger.info("Backchannel ON (experimental)")
    if os.getenv("FILLER", "off").lower() != "off":
        from .naturalness import ThinkingFiller
        extras.append(ThinkingFiller())
        logger.info("Thinking filler ON")

    pipeline = Pipeline([
        transport.input(),            # audio frames in (20ms chunks)
        stt,                          # streaming partials -> finals
        aggregators.user(),           # user turn -> context
        llm,                          # streamed tokens + tool calls
        *extras,                      # backchannel / thinking-filler (optional)
        tts,                          # first-sentence streaming synthesis
        transport.output(),           # audio out (interruptible)
        aggregators.assistant(),      # assistant turn -> context
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,     # barge-in: caller speech cancels TTS+LLM
            enable_metrics=True,          # per-stage latency (TTFT etc.) in logs
        ),
        # Feeds the live /monitor page: transcript, tool calls, per-stage latency.
        observers=[MonitorObserver()],
    )

    # Speak the greeting DIRECTLY via TTS on connect — no LLM round-trip.
    # (The old LLMRunFrame path cost ~1.5s of first-token latency just to read
    # a fixed line.) TTSSpeakFrame(append_to_context=True) also records it in
    # history so the model knows it already greeted.
    biz = ctx["business"]
    # Language-aware greeting. Bilingual ("multi") opens in ENGLISH by default,
    # then follows whichever language the caller answers in (STT auto-detects,
    # the LLM mirrors it). Spanish-only ("es") opens in Spanish.
    _greeting_en = f"Thanks for calling {biz['name']}! This is the AI assistant — how can I help?"
    _greetings = {
        "en": _greeting_en,
        "es": f"¡Gracias por llamar a {biz['name']}! Soy el asistente virtual, ¿en qué puedo ayudarle?",
        "multi": _greeting_en,
    }
    greeting = biz.get("greeting") or _greetings.get(LANGUAGE, _greetings["en"])

    @transport.event_handler("on_client_connected")
    async def _greet(transport, client):
        logger.info(f"[{call_id}] caller connected -> greeting (direct TTS)")
        await task.queue_frames([TTSSpeakFrame(greeting)])

    @transport.event_handler("on_client_disconnected")
    async def _bye(transport, client):
        logger.info(f"[{call_id}] caller disconnected")
        # Fire-and-forget call report; never blocks audio teardown.
        transcript = "\n".join(
            f"{m.get('role')}: {m.get('content')}"
            for m in context.get_messages() if isinstance(m.get("content"), str)
        )
        await handlers["_report_call"]("faq", "call ended", transcript)
        await task.cancel()

    return task
