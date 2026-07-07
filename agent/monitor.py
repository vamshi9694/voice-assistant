"""Live monitoring hub — the "log tab" for the dev webpage.

One in-process event bus fans out four kinds of events to any number of
browser tabs over Server-Sent Events (SSE):

  - log      raw loguru lines (connects, disconnects, errors, everything)
  - latency  per-stage metrics from Pipecat (STT/LLM TTFB, processing, tokens,
             TTS chars, turn-detection time) — enable_metrics=True feeds this
  - transcript  user finals + assistant responses as they happen
  - tool     each control-plane tool call the agent makes + its result

It attaches to the SAME FastAPI app the Pipecat runner already serves on
:7860, so the page lives at  http://localhost:7860/monitor  next to the call
UI. No extra server, no extra port.

Wiring (see bot.py / pipeline.py):
  - `import agent.monitor` registers the routes + the loguru sink.
  - `MonitorObserver()` is passed to the PipelineTask so it sees every frame.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from typing import Any

from loguru import logger
from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    LLMFullResponseEndFrame,
    LLMTextFrame,
    MetricsFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
)
from pipecat.metrics.metrics import (
    LLMUsageMetricsData,
    ProcessingMetricsData,
    TTFBMetricsData,
    TTSUsageMetricsData,
    TurnMetricsData,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed

# The runner exposes a module-level FastAPI app; adding routes to it here means
# they are live when bot.py calls pipecat.runner.run.main().
from pipecat.runner.run import app
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse

# When set, /monitor and /monitor/stream require ?token=<value>. Leave unset in
# local dev; set it in production so live call transcripts aren't world-readable.
MONITOR_TOKEN = os.getenv("MONITOR_TOKEN", "")


def _check_token(request: "Request") -> None:
    if MONITOR_TOKEN and request.query_params.get("token") != MONITOR_TOKEN:
        raise HTTPException(status_code=401, detail="monitor token required")

# --------------------------------------------------------------------------- #
#  Event bus
# --------------------------------------------------------------------------- #

_HISTORY = 500          # events replayed to a tab when it (re)connects
_QUEUE_MAX = 1000       # per-subscriber backlog before we drop (slow tab)


class MonitorBus:
    """Ring buffer + set of async subscriber queues. Thread/loop friendly:
    publish() is sync and never awaits, so it is safe to call from a loguru
    sink or from inside frame handlers."""

    def __init__(self) -> None:
        self._buffer: deque[dict] = deque(maxlen=_HISTORY)
        self._subscribers: set[asyncio.Queue] = set()
        self._seq = 0

    def publish(self, kind: str, **fields: Any) -> None:
        self._seq += 1
        event = {"seq": self._seq, "kind": kind, "ts": time.time(), **fields}
        self._buffer.append(event)
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Slow consumer: drop this event for them rather than block.
                pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
        # Seed with history so a freshly-opened tab isn't blank.
        for event in self._buffer:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                break
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)


bus = MonitorBus()


# --------------------------------------------------------------------------- #
#  loguru sink  ->  "log" events
# --------------------------------------------------------------------------- #

def _log_sink(message) -> None:
    r = message.record
    bus.publish(
        "log",
        level=r["level"].name,
        text=r["message"],
        module=f'{r["name"]}:{r["line"]}',
    )


# Mirror everything loguru emits into the bus. This does not replace the
# console sink the runner installs; it adds a parallel one.
logger.add(_log_sink, level="DEBUG", enqueue=False)


# --------------------------------------------------------------------------- #
#  Pipeline observer  ->  latency / transcript / tool events
# --------------------------------------------------------------------------- #

class MonitorObserver(BaseObserver):
    """Non-intrusive: reads frames as they flow, publishes structured events.

    An observer sees a frame once per processor edge, so the same frame object
    is reported many times. We dedupe on frame id to emit each event exactly
    once."""

    def __init__(self) -> None:
        super().__init__()
        self._seen: deque[int] = deque(maxlen=4096)
        self._seen_set: set[int] = set()
        self._assistant_buf: list[str] = []

    def _first_time(self, frame) -> bool:
        fid = frame.id
        if fid in self._seen_set:
            return False
        self._seen.append(fid)
        self._seen_set.add(fid)
        # Keep the set bounded in step with the deque.
        while len(self._seen_set) > self._seen.maxlen:
            self._seen_set.discard(self._seen.popleft())
        return True

    async def on_push_frame(self, data: FramePushed) -> None:
        frame = data.frame

        # ---- streamed assistant tokens: accumulate, flush on response end ----
        if isinstance(frame, LLMTextFrame):
            if self._first_time(frame):
                self._assistant_buf.append(frame.text)
            return
        if isinstance(frame, LLMFullResponseEndFrame):
            if self._assistant_buf:
                bus.publish(
                    "transcript", role="assistant",
                    text="".join(self._assistant_buf).strip(),
                )
                self._assistant_buf = []
            return

        # Everything below is emitted once per frame.
        if not self._first_time(frame):
            return

        if isinstance(frame, TranscriptionFrame):
            if frame.text and frame.text.strip():
                bus.publish("transcript", role="user", text=frame.text.strip())

        elif isinstance(frame, UserStartedSpeakingFrame):
            bus.publish("transcript", role="event", text="— caller started speaking —")

        elif isinstance(frame, BotStoppedSpeakingFrame):
            bus.publish("transcript", role="event", text="— bot finished speaking —")

        elif isinstance(frame, FunctionCallInProgressFrame):
            bus.publish(
                "tool", phase="call", name=frame.function_name,
                detail=_short(frame.arguments),
            )

        elif isinstance(frame, FunctionCallResultFrame):
            bus.publish(
                "tool", phase="result", name=frame.function_name,
                detail=_short(frame.result),
            )

        elif isinstance(frame, MetricsFrame):
            for m in frame.data:
                self._publish_metric(m)

    @staticmethod
    def _publish_metric(m) -> None:
        proc = getattr(m, "processor", "?")
        if isinstance(m, TTFBMetricsData):
            bus.publish("latency", metric="TTFB", processor=proc,
                        ms=round(m.value * 1000, 1), model=getattr(m, "model", None))
        elif isinstance(m, ProcessingMetricsData):
            bus.publish("latency", metric="processing", processor=proc,
                        ms=round(m.value * 1000, 1))
        elif isinstance(m, TurnMetricsData):
            bus.publish("latency", metric="turn", processor=proc,
                        ms=round(m.e2e_processing_time_ms, 1),
                        detail=f"complete={m.is_complete} p={m.probability:.2f}")
        elif isinstance(m, LLMUsageMetricsData):
            u = m.value
            bus.publish("latency", metric="tokens", processor=proc,
                        detail=f"in={u.prompt_tokens} out={u.completion_tokens}")
        elif isinstance(m, TTSUsageMetricsData):
            bus.publish("latency", metric="tts_chars", processor=proc,
                        detail=f"{m.value} chars")


def _short(value: Any, limit: int = 300) -> str:
    try:
        s = value if isinstance(value, str) else json.dumps(value, default=str)
    except Exception:
        s = str(value)
    return s if len(s) <= limit else s[:limit] + "…"


# --------------------------------------------------------------------------- #
#  Routes on the runner's FastAPI app
# --------------------------------------------------------------------------- #

@app.get("/monitor/stream")
async def monitor_stream(request: Request) -> StreamingResponse:
    """SSE feed of every event. Replays recent history on connect."""
    _check_token(request)
    q = bus.subscribe()

    async def gen():
        try:
            # Prompt the browser to retry quickly if the stream drops.
            yield "retry: 2000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"   # comment frame, keeps proxies happy
                    continue
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/monitor", response_class=HTMLResponse)
async def monitor_page(request: Request) -> str:
    _check_token(request)
    return _PAGE


# HTML lives in a sibling file so this module stays readable.
import pathlib as _pathlib  # noqa: E402

_PAGE = (_pathlib.Path(__file__).parent / "monitor.html").read_text(encoding="utf-8")
