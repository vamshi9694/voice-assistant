"""Naturalness processors — make the agent feel more human.

Two small pipeline processors, both OFF by default (enabled by env flags in
pipeline.py). They only ever ADD short spoken frames; they never drop or alter
the real conversation, so they're low blast-radius.

  ThinkingFiller  — when the LLM starts a tool call (availability/booking, the
                    turns with real latency), speak a quick "let me check that"
                    so the caller isn't sitting in silence. Guarantees the
                    filler even if the model forgets to say one.

  Backchannel     — EXPERIMENTAL. A brief "mm-hm"/"okay" after the caller
                    pauses, to signal listening. Risky on a phone line (the bot
                    can hear its own audio / feel like it's interrupting), so
                    it's off by default and probabilistic. Test carefully.
"""
from __future__ import annotations

import random

from pipecat.frames.frames import (
    Frame,
    FunctionCallInProgressFrame,
    TTSSpeakFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

# Short, warm, varied. Kept brief so they don't add much latency of their own.
FILLER_PHRASES = [
    "Let me check that for you.",
    "One moment, let me take a look.",
    "Sure, let me pull that up.",
    "Okay, give me one sec.",
    "Alright, let me check.",
]

BACKCHANNEL_PHRASES = ["Mm-hm.", "Right.", "Got it.", "Okay.", "Sure."]


class ThinkingFiller(FrameProcessor):
    """Speak a filler the instant a tool call begins (covers the lookup wait).

    Placed between the LLM and TTS. On a FunctionCallInProgressFrame it pushes a
    short TTSSpeakFrame downstream, then forwards the original frame untouched.
    """

    def __init__(self, phrases: list[str] | None = None) -> None:
        super().__init__()
        self._phrases = phrases or FILLER_PHRASES

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, FunctionCallInProgressFrame):
            await self.push_frame(
                TTSSpeakFrame(random.choice(self._phrases)), FrameDirection.DOWNSTREAM
            )

        await self.push_frame(frame, direction)


class Backchannel(FrameProcessor):
    """EXPERIMENTAL. Occasionally emit a brief acknowledgment when the caller
    pauses. Off by default; probability-gated so it doesn't fire every pause.

    Placed between the LLM and TTS. It never blocks or drops frames — worst case
    it says "mm-hm" at an awkward moment, which is why it's opt-in and tunable.
    """

    def __init__(self, probability: float = 0.3, phrases: list[str] | None = None) -> None:
        super().__init__()
        self._probability = probability
        self._phrases = phrases or BACKCHANNEL_PHRASES

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)

        if isinstance(frame, UserStoppedSpeakingFrame) and random.random() < self._probability:
            await self.push_frame(
                TTSSpeakFrame(random.choice(self._phrases)), FrameDirection.DOWNSTREAM
            )
