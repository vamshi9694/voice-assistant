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
from .qa import QAObserver
from .tools import make_handlers, tools

CONTROL_PLANE = os.getenv("CONTROL_PLANE_URL", "http://127.0.0.1:8080")
STACK = os.getenv("STACK", "local")
# Language: "en" | "es" | "multi" (bilingual auto-detect — Deepgram detects
# English vs Spanish per utterance, incl. code-switching; the LLM replies in
# whichever the caller used). ENV VALUE IS ONLY A FALLBACK — per-tenant
# language config from /agent/{slug}/context wins (see resolve_language()).
LANGUAGE = os.getenv("LANGUAGE", "en").lower()


def resolve_language(ctx: dict) -> str:
    """Per-tenant language mode from the control-plane context.

    - auto_detect + >1 enabled language -> "multi" (STT auto-detects per utterance)
    - otherwise the tenant's default language
    - tenants without language config (old rows) -> LANGUAGE env fallback
    """
    langs = ctx.get("languages") or {}
    if not langs:
        return LANGUAGE
    enabled = langs.get("enabled") or [langs.get("default", "en")]
    if langs.get("auto_detect") and len(enabled) > 1:
        return "multi"
    return (langs.get("default") or "en").lower()
# Semantic endpointing. "local" = Smart Turn v3 (best perceived latency, but
# pulls torch + a model download — heavy for a cloud image). "off" = rely on
# Silero VAD + the STT provider's endpointing (Deepgram has its own), which
# keeps the hosted image lean and cold-starts fast. Silero VAD is ALWAYS on.
SMART_TURN = os.getenv("SMART_TURN", "local").lower()


def build_services(language: str = "en", voices: dict | None = None):
    """Swappable model layer — the two-tier strategy in code.

    language: per-tenant mode ("en"/"es"/"multi") from resolve_language().
    voices:   per-tenant {lang: voice_id} map (Business.voice_map). The voice
              for the tenant's primary language wins; env vars are fallback.
    """
    voices = voices or {}
    primary = language if language != "multi" else "en"
    if STACK == "hosted":
        from pipecat.services.cartesia.tts import CartesiaTTSService, GenerationConfig
        from pipecat.services.deepgram.stt import DeepgramSTTService
        from pipecat.transcriptions.language import Language

        dg_lang = {"en": "en-US", "es": "es", "multi": "multi"}.get(language, "en-US")
        stt = DeepgramSTTService(
            api_key=os.getenv("DEEPGRAM_API_KEY"),
            # numerals: "one two three" -> "123" — essential for phone numbers,
            # party sizes, times. smart_format also tidies dates/numbers.
            # language: "multi" auto-detects English/Spanish (nova-3), so one
            # number serves both; "es" locks Spanish, "en-US" locks English.
            settings=DeepgramSTTService.Settings(
                numerals=True,
                smart_format=True,
                language=dg_lang,
                # Number-aware endpointing: wait ~0.3s of silence before Deepgram
                # finalizes, so a caller pausing between digit groups ("939…404…
                # 999") stays ONE utterance instead of being chopped into three
                # (the main cause of mangled phone numbers). utterance_end_ms is
                # Deepgram's end-of-speech safety net. Smart Turn still decides
                # the actual turn, so this adds little perceived latency.
                endpointing=int(os.getenv("DG_ENDPOINTING_MS", "300")),
                utterance_end_ms=int(os.getenv("DG_UTTERANCE_END_MS", "1000")),
            ),
        )
        # LLM provider. Groq (Llama 3.3 70B) has ~sub-200ms, consistent time-to-
        # first-token — the fix for OpenAI's spiky rate-tier latency. Both support
        # tool calling. LLM_PROVIDER=groq | openai (default).
        if os.getenv("LLM_PROVIDER", "openai").lower() == "groq":
            from pipecat.services.groq.llm import GroqLLMService
            llm = GroqLLMService(
                api_key=os.getenv("GROQ_API_KEY"),
                model=os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
            )
            logger.info(f"LLM: Groq {os.getenv('GROQ_MODEL', 'llama-3.3-70b-versatile')}")
        else:
            from pipecat.services.openai.llm import OpenAILLMService
            llm = OpenAILLMService(model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"))
            logger.info(f"LLM: OpenAI {os.getenv('OPENAI_MODEL', 'gpt-4o-mini')}")
        # Cartesia synthesis language. Bilingual ("multi") defaults to English;
        # sonic-3.5 is multilingual so Spanish replies still render acceptably
        # through the same voice (set a per-language voice in the tenant's
        # voice_map for perfect pronunciation).
        _tts_lang = {"en": Language.EN, "es": Language.ES, "multi": Language.EN}.get(
            language, Language.EN
        )
        tts = CartesiaTTSService(
            api_key=os.getenv("CARTESIA_API_KEY"),
            voice_id=voices.get(primary) or os.getenv("CARTESIA_VOICE_ID", ""),
            # TTS_SPEED (default 1.05): slightly brisk & businesslike. A slower
            # speed (<1.0) reads as sultry/"flirty" on the phone, so we lean a
            # touch fast. Range 0.6–1.5; tune per taste without a code change.
            params=CartesiaTTSService.InputParams(
                language=_tts_lang,
                generation_config=GenerationConfig(speed=float(os.getenv("TTS_SPEED", "1.05"))),
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
        tts = KokoroTTSService(voice_id=voices.get(primary) or os.getenv("KOKORO_VOICE", "af_heart"))
    return stt, llm, tts


def transport_audio_params() -> dict:
    """Audio config shared by every transport.

    VAD + turn detection are NOT configured here (see module note above) —
    they're attached to the user context aggregator in build_call_task().

    AMBIANCE_FILE (optional): path to a looping background sound (e.g. faint
    restaurant murmur) mixed UNDER the bot's voice so the call feels like a real
    place answered — the trick Vapi uses. Keep AMBIANCE_VOLUME low (~0.1–0.2).
    """
    params = dict(audio_in_enabled=True, audio_out_enabled=True)

    # Input denoising (Krisp-style): RNNoise cleans background noise off the
    # CALLER's audio before STT — cuts TV/chatter/echo so the bot mishears less.
    # Off by default; enable with DENOISE=on. Guarded so a missing dep can never
    # break a call — it just runs without denoise.
    if os.getenv("DENOISE", "off").lower() != "off":
        try:
            import pyrnnoise  # noqa: F401  (presence check)
            from pipecat.audio.filters.rnnoise_filter import RNNoiseFilter

            params["audio_in_filter"] = RNNoiseFilter()
            logger.info("Input denoising ON (RNNoise)")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"DENOISE requested but unavailable ({type(e).__name__}); running without it")

    ambiance = os.getenv("AMBIANCE_FILE", "")
    if ambiance:
        from pipecat.audio.mixers.soundfile_mixer import SoundfileMixer

        params["audio_out_mixer"] = SoundfileMixer(
            sound_files={"ambiance": ambiance},
            default_sound="ambiance",
            volume=float(os.getenv("AMBIANCE_VOLUME", "0.15")),
            loop=True,
        )
        logger.info(f"Background ambiance ON: {ambiance}")

    return params


async def fetch_business_context(slug: str) -> dict:
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(f"{CONTROL_PLANE}/agent/{slug}/context")
        r.raise_for_status()
        return r.json()


async def build_call_task(
    transport, slug: str, call_id: str | None = None,
    caller_phone: str = "", called_number: str = "",
) -> PipelineTask:
    """Assemble the per-call pipeline: one call = one task = one worker.

    Tenant isolation: `ctx` is fetched for THIS slug only; every tool handler
    closes over THIS slug; nothing tenant-scoped comes from process env."""
    call_id = call_id or str(uuid.uuid4())
    ctx = await fetch_business_context(slug)

    language = resolve_language(ctx)
    voices = (ctx.get("languages") or {}).get("voices") or {}
    logger.info(f"[{call_id}] language mode '{language}' voices={voices or '(env default)'}")
    stt, llm, tts = build_services(language=language, voices=voices)

    # FLOWS=on runs the booking as a state machine (agent/flow.py); the flow
    # registers per-node functions itself. Otherwise use the single big prompt
    # + globally-registered tools (the default, battle-tested path).
    FLOWS = os.getenv("FLOWS", "off").lower() != "off"
    handlers = make_handlers(slug, call_id)  # _report_call used at disconnect either way

    if FLOWS:
        logger.info("FLOWS ON — booking runs as a state machine")
        context = LLMContext(messages=[])   # flow manages system prompt + functions per node
    else:
        for name, fn in handlers.items():
            if not name.startswith("_"):
                llm.register_function(name, fn)
        system_prompt = build_system_prompt(ctx, datetime.now(), language=language)
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

    # Phase A — smart barge-in: while the bot is speaking, require BARGEIN_MIN_WORDS
    # spoken words before the caller can interrupt, so brief "okay/yeah/mm-hm"
    # don't cut it off (real interruptions like "stop/wait/no" clear the bar).
    # The threshold applies ONLY during bot speech — short answers ("yes") still
    # register instantly. 0 = off (current behaviour, untouched).
    bargein = int(os.getenv("BARGEIN_MIN_WORDS", "0"))
    start_strats = None
    if bargein > 0:
        from pipecat.turns.user_start.min_words_user_turn_start_strategy import (
            MinWordsUserTurnStartStrategy,
        )
        start_strats = [MinWordsUserTurnStartStrategy(min_words=bargein)]
        logger.info(f"Smart barge-in ON: min_words={bargein} (to interrupt the bot)")

    stop_strats = None
    if SMART_TURN != "off":
        from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
        from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy

        stop_strats = [TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3())]
    else:
        logger.info("Smart Turn disabled (SMART_TURN=off) — using VAD + STT endpointing")

    if start_strats is not None or stop_strats is not None:
        from pipecat.turns.user_turn_strategies import (
            UserTurnStrategies,
            default_user_turn_start_strategies,
            default_user_turn_stop_strategies,
        )
        user_params.user_turn_strategies = UserTurnStrategies(
            start=start_strats or default_user_turn_start_strategies(),
            stop=stop_strats or default_user_turn_stop_strategies(),
        )

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
        # REQUEST_DELAYED_SECS>0 adds a "still working..." line if a tool runs
        # longer than that many seconds (Phase C). 0 = just the instant filler.
        extras.append(ThinkingFiller(delay_secs=float(os.getenv("REQUEST_DELAYED_SECS", "0"))))
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

    # Phase B — idle/silence handling. If the caller goes quiet for
    # IDLE_PROMPT_SECS, the pipeline fires on_idle_timeout: first time we ask
    # "are you still there?", second time we say a friendly goodbye and end the
    # call (instead of leaving a dead line open). 0 = off.
    idle_secs = int(os.getenv("IDLE_PROMPT_SECS", "0"))
    idle_kwargs = (
        dict(idle_timeout_secs=float(idle_secs), cancel_on_idle_timeout=False)
        if idle_secs > 0 else {}
    )

    qa = QAObserver(slug, call_id)
    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,     # barge-in: caller speech cancels TTS+LLM
            enable_metrics=True,          # per-stage latency (TTFT etc.) in logs
        ),
        # MonitorObserver feeds the live /monitor page; QAObserver persists
        # latency + dead-air/"hello?"/tool-failure events to the control plane.
        observers=[MonitorObserver(), qa],
        **idle_kwargs,
    )

    if idle_secs > 0:
        from pipecat.frames.frames import EndFrame
        _idle_state = {"n": 0}

        @task.event_handler("on_idle_timeout")
        async def _on_idle(task):
            n = _idle_state["n"]
            _idle_state["n"] += 1
            if n == 0:
                logger.info(f"[{call_id}] idle -> 'are you still there?'")
                await task.queue_frames([TTSSpeakFrame("Sorry, are you still there?")])
            else:
                logger.info(f"[{call_id}] idle again -> ending call")
                await task.queue_frames([
                    TTSSpeakFrame("I'll let you go for now — thanks for calling, goodbye!"),
                    EndFrame(),
                ])

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
    greeting = biz.get("greeting") or _greetings.get(language, _greetings["en"])

    # State-machine mode: build the FlowManager now, initialize it on connect
    # (after the greeting) so the flow drives the conversation from turn one.
    flow_manager = None
    if FLOWS:
        from pipecat.flows import FlowManager
        flow_manager = FlowManager(
            llm=llm, context_aggregator=aggregators, worker=task, transport=transport,
        )

    @transport.event_handler("on_client_connected")
    async def _greet(transport, client):
        logger.info(f"[{call_id}] caller connected -> greeting (direct TTS)")
        await task.queue_frames([TTSSpeakFrame(greeting)])
        if flow_manager is not None:
            from .flow import build_initial_node
            await flow_manager.initialize(
                build_initial_node(ctx, slug, call_id, language, datetime.now())
            )

    @transport.event_handler("on_client_disconnected")
    async def _bye(transport, client):
        logger.info(f"[{call_id}] caller disconnected")
        # Fire-and-forget call report; never blocks audio teardown.
        transcript = "\n".join(
            f"{m.get('role')}: {m.get('content')}"
            for m in context.get_messages() if isinstance(m.get("content"), str)
        )
        await qa.flush()   # persist any buffered QA events before teardown
        await handlers["_report_call"](
            "faq", "call ended", transcript,
            caller_phone=caller_phone, called_number=called_number, language=language,
        )
        await task.cancel()

    return task
