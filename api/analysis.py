"""Phase D — end-of-call analysis.

A background LLM pass over the finished transcript that produces a clean
summary, an outcome, a success judgement, sentiment, and extracted key details
(name, party size, phone, items…). Mirrors Vapi's post-call "analysis plan".

Graceful: if OPENAI_API_KEY is unset (or the call fails), returns {} and the
CallRecord keeps its defaults — nothing breaks.
"""
from __future__ import annotations

import json
import os

import httpx
from loguru import logger

OUTCOMES = ["faq", "reservation", "order", "message", "transfer", "junk", "abandoned"]

_SCHEMA_HINT = (
    '{"summary": "one plain sentence describing what happened", '
    f'"outcome": one of {OUTCOMES}, '
    '"success": true or false (did the caller get what they needed), '
    '"sentiment": "positive" | "neutral" | "negative", '
    '"caller_intent": "short phrase", '
    '"details": {"name": "", "party_size": null, "date": "", "time": "", '
    '"phone": "", "items": [], "notes": ""}}'
)


async def analyze_call(transcript: str) -> dict:
    """Return the structured analysis dict, or {} if unavailable."""
    key = os.getenv("OPENAI_API_KEY")
    if not key or not (transcript or "").strip():
        return {}

    model = os.getenv("OPENAI_ANALYSIS_MODEL") or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    prompt = (
        "You analyze a restaurant phone-call transcript. Reply with ONLY a JSON "
        "object of this exact shape (no prose):\n" + _SCHEMA_HINT +
        "\n\nOmit any detail you can't find. TRANSCRIPT:\n" + transcript[:6000]
    )
    try:
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"authorization": f"Bearer {key}"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": {"type": "json_object"},
                    "temperature": 0,
                },
            )
        if r.status_code != 200:
            logger.warning(f"call analysis LLM {r.status_code}: {r.text[:150]}")
            return {}
        return parse_analysis(r.json()["choices"][0]["message"]["content"])
    except Exception as e:  # noqa: BLE001
        logger.warning(f"call analysis failed: {type(e).__name__}: {e}")
        return {}


def parse_analysis(content: str) -> dict:
    """Parse + sanitize the LLM JSON. Pure function → unit-testable offline."""
    try:
        data = json.loads(content)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out = {
        "summary": str(data.get("summary", ""))[:400],
        "caller_intent": str(data.get("caller_intent", ""))[:200],
        "details": data.get("details") if isinstance(data.get("details"), dict) else {},
    }
    oc = data.get("outcome")
    out["outcome"] = oc if oc in OUTCOMES else None
    out["success"] = data.get("success") if isinstance(data.get("success"), bool) else None
    s = str(data.get("sentiment", "")).lower()
    out["sentiment"] = s if s in ("positive", "neutral", "negative") else ""
    return out
