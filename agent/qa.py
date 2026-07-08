"""Call QA observer — turns pipeline frames into persisted CallEvent metrics.

Watches the same frame stream as the live monitor, but instead of feeding a
dev webpage it ships structured events to the control plane:

  stt_ttfb / llm_ttfb / tts_ttfb / turn_e2e   per-stage latency (MetricsFrame)
  tool_latency / tool_failure                 tool round-trips + errors
  dead_air                                    no bot audio > DEAD_AIR_MS after caller stops
  hello_retry                                 caller said "hello?" / "are you there?"
  low_confidence                              STT confidence below floor (when provided)

Bad-call detectors (agent/guard.py rules — these are the alerts that catch a
bot lying to a caller):
  fake_claim          bot said "confirmed"/"total is"/"connecting you"/"message
                      sent" with NO matching successful tool call this call
  stall_no_tool       bot said "let me check" but no tool ran before the next turn
  style_violation     banned chatbot-y phrase ("awesome", "hey there", "just an AI")
  clarification_loop  3+ consecutive bot re-asks — caller is stuck
  caller_distrust     caller said "you're lying" / "that's not what I said"

Never blocks or raises into the audio path: events buffer in memory and flush
in a fire-and-forget task; a dead control plane just drops metrics.
"""
import asyncio
import os
import re
import time

import httpx
from loguru import logger

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    MetricsFrame,
    TranscriptionFrame,
    TTSTextFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.metrics.metrics import TTFBMetricsData, TurnMetricsData
from pipecat.observers.base_observer import BaseObserver, FramePushed

from . import guard

CONTROL_PLANE = os.getenv("CONTROL_PLANE_URL", "http://127.0.0.1:8080")
DEAD_AIR_MS = float(os.getenv("DEAD_AIR_MS", "2500"))
CONFIDENCE_FLOOR = float(os.getenv("STT_CONFIDENCE_FLOOR", "0.55"))
FLUSH_AT = 8

HELLO_RE = re.compile(
    r"^\s*(hello+|hey|hi|hola|alo+|bueno)\s*[?!.]*\s*$"
    r"|are you (still )?there|can you hear me|you there\b|is (anyone|anybody) there"
    r"|sigues? ah[ií]|me escuchas",
    re.I,
)

DISTRUST_RE = re.compile(
    r"you'?re lying|that'?s not (true|right|what i said)|you (just )?said"
    r"|i didn'?t say that|stop making (things|stuff) up|that'?s wrong",
    re.I,
)


class QAObserver(BaseObserver):
    """Attach per call: QAObserver(slug, call_id). Call `await flush()` at
    call end (pipeline wires this into the disconnect handler)."""

    def __init__(self, slug: str, call_id: str):
        super().__init__()
        self._slug = slug
        self._call_id = call_id or ""
        self._buf: list[dict] = []
        self._seen: set[int] = set()
        self._tool_started: dict[str, float] = {}
        self._user_stopped_at: float | None = None
        self._dead_air_flagged = False
        self._greeted = False
        # bad-call detection state (guard.py rules)
        self._tools_ok: set[str] = set()        # tools that SUCCEEDED this call
        self._bot_turn_text: list[str] = []     # TTS text of the current bot turn
        self._clarify_strikes = 0               # consecutive bot re-ask turns
        self._stall_pending: str | None = None  # "let me check" awaiting a tool

    # ------------------------------ shipping ------------------------------

    def _emit(self, kind: str, value_ms: float | None = None, detail: str = ""):
        self._buf.append({"call_id": self._call_id, "kind": kind,
                          "value_ms": round(value_ms, 1) if value_ms is not None else None,
                          "detail": detail[:300]})
        if len(self._buf) >= FLUSH_AT:
            asyncio.get_event_loop().create_task(self.flush())

    async def flush(self):
        if not self._buf:
            return
        batch, self._buf = self._buf, []
        try:
            async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
                await client.post(f"{CONTROL_PLANE}/agent/{self._slug}/metrics", json=batch)
        except Exception as e:  # noqa: BLE001 — metrics must never hurt a call
            logger.warning(f"[{self._call_id}] metrics flush failed: {type(e).__name__}")

    # ------------------------------ frames ------------------------------

    async def on_push_frame(self, data: FramePushed):
        frame = data.frame
        if frame.id in self._seen:
            return
        self._seen.add(frame.id)
        now = time.monotonic()

        if isinstance(frame, UserStoppedSpeakingFrame):
            self._user_stopped_at = now
            self._dead_air_flagged = False

        elif isinstance(frame, BotStartedSpeakingFrame):
            if self._user_stopped_at is not None:
                gap_ms = (now - self._user_stopped_at) * 1000
                if gap_ms > DEAD_AIR_MS:
                    self._emit("dead_air", gap_ms, "bot audio late after caller stopped")
                self._user_stopped_at = None
            self._greeted = True

        elif isinstance(frame, UserStartedSpeakingFrame):
            # caller resumed while we owed them audio -> they were left hanging
            if (self._user_stopped_at is not None and not self._dead_air_flagged
                    and (now - self._user_stopped_at) * 1000 > DEAD_AIR_MS):
                self._emit("dead_air", (now - self._user_stopped_at) * 1000,
                           "caller spoke again before bot responded")
                self._dead_air_flagged = True

        elif isinstance(frame, TTSTextFrame):
            self._bot_turn_text.append(frame.text or "")

        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._check_bot_turn(" ".join(self._bot_turn_text).strip())
            self._bot_turn_text = []

        elif isinstance(frame, TranscriptionFrame):
            text = (frame.text or "").strip()
            if text and self._greeted and HELLO_RE.search(text):
                self._emit("hello_retry", None, text)
            if text and DISTRUST_RE.search(text):
                self._emit("caller_distrust", None, text)
            conf = _confidence_of(frame)
            if conf is not None and conf < CONFIDENCE_FLOOR:
                self._emit("low_confidence", round(conf * 1000),
                           f"conf={conf:.2f} text={text[:80]}")

        elif isinstance(frame, FunctionCallInProgressFrame):
            self._tool_started[frame.tool_call_id or frame.function_name] = now
            self._stall_pending = None  # a tool IS running — "let me check" was honest

        elif isinstance(frame, FunctionCallResultFrame):
            key = frame.tool_call_id or frame.function_name
            started = self._tool_started.pop(key, None)
            if started is not None:
                self._emit("tool_latency", (now - started) * 1000, frame.function_name)
            result = frame.result if isinstance(frame.result, dict) else {}
            if isinstance(result, dict) and result.get("error"):
                self._emit("tool_failure", None,
                           f"{frame.function_name}: {str(result.get('error'))[:120]}")
            if guard.tool_success(frame.function_name, frame.result):
                self._tools_ok.add(frame.function_name)

        elif isinstance(frame, MetricsFrame):
            for m in frame.data:
                proc = str(getattr(m, "processor", "")).lower()
                if isinstance(m, TTFBMetricsData):
                    ms = m.value * 1000
                    if "stt" in proc or "transcri" in proc or "whisper" in proc or "deepgram" in proc:
                        self._emit("stt_ttfb", ms, proc)
                    elif "llm" in proc or "openai" in proc or "ollama" in proc:
                        self._emit("llm_ttfb", ms, proc)
                    elif "tts" in proc or "cartesia" in proc or "kokoro" in proc:
                        self._emit("tts_ttfb", ms, proc)
                elif isinstance(m, TurnMetricsData):
                    self._emit("turn_e2e", m.e2e_processing_time_ms, proc)

    # ------------------------- bad-call detection -------------------------

    def _check_bot_turn(self, text: str):
        """Run guard.py detectors over one completed bot utterance."""
        if not text:
            return
        # 1. Success claims with no successful tool behind them = the bot lied.
        for kind in guard.unverified_claims(text, self._tools_ok):
            self._emit("fake_claim", None, f"{kind}: {text[:200]}")
        # 2. "Let me check" with no tool running: if the previous turn promised
        #    a check and no tool started since, that promise was empty.
        if self._stall_pending:
            self._emit("stall_no_tool", None, self._stall_pending[:200])
            self._stall_pending = None
        if guard.is_stall(text) and not self._tool_started:
            self._stall_pending = text  # cleared if a tool starts before next turn
        # 3. Banned chatbot-y phrases (tone regression tracking).
        for phrase in guard.style_violations(text):
            self._emit("style_violation", None, f"{phrase!r} in: {text[:150]}")
        # 4. Clarification loop: 3+ consecutive re-asks means the caller is stuck
        #    and the bot should have summarized + offered a message instead.
        if guard.is_clarification(text):
            self._clarify_strikes += 1
            if self._clarify_strikes > guard.MAX_CLARIFY_STRIKES:
                self._emit("clarification_loop", None,
                           f"{self._clarify_strikes} consecutive re-asks: {text[:150]}")
        else:
            self._clarify_strikes = 0


def _confidence_of(frame) -> float | None:
    """Deepgram exposes confidence on the transcription result; local Whisper
    doesn't. Look in the usual places, return None when unavailable."""
    for attr in ("confidence",):
        v = getattr(frame, attr, None)
        if isinstance(v, (int, float)):
            return float(v)
    result = getattr(frame, "result", None)
    if result is not None:
        v = getattr(result, "confidence", None)
        if isinstance(v, (int, float)):
            return float(v)
        try:
            alts = result["channel"]["alternatives"]
            return float(alts[0]["confidence"])
        except Exception:  # noqa: BLE001
            pass
    return None
