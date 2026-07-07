"""Startup warm-up for the local stack — moves cold-start cost off the call path.

Two real per-process costs, neither fixed by touching the pipecat service
objects (they're rebuilt fresh per call in build_services(), but that's
cheap — the expensive state lives elsewhere):

  - Ollama unloads a model from memory after ~5 minutes idle. Whoever places
    the first call after a gap pays the full model-load time as LLM TTFB.
  - Whisper-MLX loads its weights lazily on first transcription, but
    mlx_whisper caches them at the process level after that (see
    mlx_whisper.transcribe.ModelHolder) — only the very first call in this
    process's lifetime is slow.

Firing one throwaway request at each on startup pays both costs before the
phone can ring instead of on the caller's first turn.
"""
import asyncio
import os

import httpx
import numpy as np
from loguru import logger

from pipecat.runner.run import app

STACK = os.getenv("STACK", "local")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/v1")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:14b")
# Ollama's own default is 5m; keep it loaded for a full shift so gaps between
# calls don't force a reload. "-1" would mean "never unload".
OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "30m")


async def _warm_ollama() -> None:
    native_base = OLLAMA_URL.removesuffix("/v1")
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            # Empty prompt: Ollama loads the model into memory without
            # generating anything.
            r = await client.post(
                f"{native_base}/api/generate",
                json={
                    "model": OLLAMA_MODEL,
                    "prompt": "",
                    "keep_alive": OLLAMA_KEEP_ALIVE,
                },
            )
            r.raise_for_status()
        logger.info(f"[warmup] Ollama model loaded: {OLLAMA_MODEL}")
    except Exception as e:
        logger.warning(f"[warmup] Ollama warm-up failed (first call will pay the load cost): {e}")


async def _warm_whisper() -> None:
    from pipecat.services.whisper.stt import MLXModel

    model = os.getenv("WHISPER_MLX_MODEL", MLXModel.LARGE_V3_TURBO_Q4.value)
    try:
        import mlx_whisper

        silence = np.zeros(16000, dtype=np.float32)  # 1s @ 16kHz
        await asyncio.to_thread(mlx_whisper.transcribe, silence, path_or_hf_repo=model)
        logger.info(f"[warmup] Whisper-MLX model loaded: {model}")
    except Exception as e:
        logger.warning(f"[warmup] Whisper warm-up failed (first call will pay the load cost): {e}")


@app.on_event("startup")
async def _warm_local_stack() -> None:
    if STACK != "local":
        return
    logger.info("[warmup] warming local STT/LLM models...")
    await asyncio.gather(_warm_ollama(), _warm_whisper())
