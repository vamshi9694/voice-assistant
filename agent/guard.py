"""Guardrails — deterministic, prompt-independent safety logic.

The principle (same one Vapi/Retell build around): the prompt is probabilistic;
anything the model must not be able to fake is enforced in code. This module is
pure Python (no pipecat imports) so it's unit-testable anywhere and shared by:

  tools.py     phone validation BEFORE any mutating tool hits the backend
  flow.py      slot validation inside the state machine
  qa.py        fake-claim / style / clarification-loop detection on bot speech
  simulate.py  scenario acceptance checks

Two enforcement tiers:
  1. HARD GATES  — invalid input never reaches the backend (invalid phone,
                   missing slots). The tool returns a structured error the LLM
                   must speak around by re-asking.
  2. DETECTORS   — the bot's spoken text is scanned for success claims
                   ("you're all set", "your total is", "connecting you now",
                   "message sent"). A claim with no matching successful tool
                   call this call = a `fake_claim` QA event (alert + review).
"""
from __future__ import annotations

import re

# ------------------------------ phone numbers ------------------------------


def normalize_phone(raw: str) -> str:
    """Digits only; keep a leading + for international. '(665) 493-1454' -> '6654931454'."""
    if not raw:
        return ""
    digits = "".join(ch for ch in raw if ch.isdigit())
    return ("+" + digits) if raw.strip().startswith("+") else digits


def valid_phone(raw: str) -> bool:
    """Callback-able number: 10 digits (US), 11 starting with 1, or +intl 11–15."""
    p = normalize_phone(raw)
    if p.startswith("+"):
        return 11 <= len(p) - 1 <= 15
    if len(p) == 11 and p.startswith("1"):
        return True
    return len(p) == 10 and p[0] not in "01"


def phone_problem(raw: str) -> str | None:
    """None when valid; otherwise a short description the LLM can speak around."""
    p = normalize_phone(raw)
    if not p:
        return "no phone number provided"
    if valid_phone(raw):
        return None
    n = len(p.lstrip("+"))
    if n < 10:
        return f"only {n} digits heard — a US number needs 10; ask for the missing digits"
    return f"{n} digits heard — that isn't a valid callback number; ask them to repeat it"


# ------------------------- success-claim detection -------------------------
# claim kind -> spoken patterns that assert a completed business action.
CLAIM_PATTERNS: dict[str, re.Pattern] = {
    "reservation_confirmed": re.compile(
        r"\b(you'?re all set|all set for|reservation (is|has been) (confirmed|booked|made)"
        r"|i'?ve (booked|confirmed|reserved)|booking (is|has been) confirmed"
        r"|table (is|has been) (booked|reserved|confirmed)|see you (then|on)\b.*\bconfirmed)\b",
        re.I,
    ),
    "order_total": re.compile(
        r"\b(your total (is|comes to)|that comes to|the total (is|comes to)"
        r"|total of \$|order total)\b",
        re.I,
    ),
    "transfer_started": re.compile(
        r"\b(connect(ing)? you|transfer(ring)? you|put(ting)? you through"
        r"|patch(ing)? you (through|over)|hand(ing)? you (over|off))\b",
        re.I,
    ),
    "message_sent": re.compile(
        r"\b(message (has been|was|is) (sent|passed|delivered|recorded)"
        r"|i'?ve (sent|passed|left) (that|your message|the message|it|a note)"
        r"|i'?ve let (them|the (team|manager|staff)) know"
        r"|(sent|passed) (that|it) (along|on|over) to)\b",
        re.I,
    ),
    "order_placed": re.compile(
        r"\b(order (is|has been) (placed|confirmed|in|sent to the kitchen)"
        r"|i'?ve (placed|put in|sent) (your|the) order"
        r"|kitchen (has|received) (it|your order))\b",
        re.I,
    ),
}

# claim kind -> (tool name, success predicate on the tool's result dict)
CLAIM_REQUIRES: dict[str, tuple[str, ...]] = {
    "reservation_confirmed": ("create_reservation",),
    "order_total": ("create_order", "calculate_order_total"),
    "transfer_started": ("transfer_call",),
    "message_sent": ("take_message", "save_message"),
    "order_placed": ("create_order",),
}


def tool_success(name: str, result) -> bool:
    """True only when the backend really did the thing."""
    if not isinstance(result, dict) or result.get("error"):
        return False
    if name in ("create_reservation", "create_order", "take_message", "save_message"):
        return bool(result.get("created")) or bool(result.get("ok"))
    if name == "transfer_call":
        return bool(result.get("transferred"))
    if name == "calculate_order_total":
        return "total" in result
    if name == "check_availability":
        return "available" in result
    return True  # read-only tools (search_knowledge etc.)


def detect_claims(text: str) -> set[str]:
    """Which business-success claims does this bot utterance make?"""
    return {kind for kind, pat in CLAIM_PATTERNS.items() if pat.search(text or "")}


def unverified_claims(text: str, succeeded_tools: set[str]) -> set[str]:
    """Claims in `text` not backed by any successful tool call so far this call."""
    return {
        kind for kind in detect_claims(text)
        if not any(t in succeeded_tools for t in CLAIM_REQUIRES.get(kind, ()))
    }


# ------------------------------ style linting ------------------------------
# Front-desk tone: calm, short, professional. These read as chatbot-y or
# unprofessional on a phone line; flagged as QA events for prompt iteration.
BANNED_STYLE = re.compile(
    r"\b(awesome|perfect|great choice|hey there|no problem at all"
    r"|i'?m (just|only) an ai|as an ai|amazing)\b|!{2,}",
    re.I,
)

# "let me check"-style stall phrases: only a problem when spoken WITHOUT a tool
# actually running (dead promise -> caller waits on nothing).
STALL_PHRASES = re.compile(
    r"\b(let me (check|look|see|pull that up)|one (moment|second|sec)"
    r"|give me a (moment|second)|hold on|checking (that|now))\b",
    re.I,
)

# Bot re-ask / didn't-catch phrases — used to count clarification strikes.
CLARIFY_PHRASES = re.compile(
    r"\b(didn'?t (quite )?catch|could you (repeat|say that again)|say that (again|one more time)"
    r"|sorry,? (what|come again)|i'?m having trouble (hearing|understanding)"
    r"|one more time|repeat that)\b",
    re.I,
)

MAX_CLARIFY_STRIKES = 2  # after 2 failed attempts: summarize + offer message/escalation


def style_violations(text: str) -> list[str]:
    return [m.group(0) for m in BANNED_STYLE.finditer(text or "")]


def is_clarification(text: str) -> bool:
    return bool(CLARIFY_PHRASES.search(text or ""))


def is_stall(text: str) -> bool:
    return bool(STALL_PHRASES.search(text or ""))
