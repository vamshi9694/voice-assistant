"""Unit tests for the deterministic guard layer (agent/guard.py) and the
prompt safety rules. Pure Python — no pipecat, no network, runs anywhere:

    python -m pytest tests/ -q

These encode the acceptance criteria:
  - No fake booking confirmations / order totals / manager transfers.
  - No invalid phone accepted.
  - No hallucinated menu/prices (prompt grounding rules present).
  - Max 2 clarification attempts before offering message/escalation.
  - One question at a time; professional, calm tone rules present.
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import guard  # noqa: E402
from agent.prompts import build_system_prompt  # noqa: E402

# ------------------------------ phone validation ------------------------------


def test_valid_us_phone_formats():
    for raw in ("6654931454", "(665) 493-1454", "665-493-1454", "665 493 1454",
                "16654931454", "+16654931454", "+447911123456"):
        assert guard.valid_phone(raw), raw


def test_invalid_phones_rejected():
    for raw in ("", "12345", "123456789", "0654931454", "555", "not a number",
                "66549314541234567890"):
        assert not guard.valid_phone(raw), raw
        assert guard.phone_problem(raw) is not None, raw


def test_normalize_phone():
    assert guard.normalize_phone("(665) 493-1454") == "6654931454"
    assert guard.normalize_phone("+1 665 493 1454") == "+16654931454"
    assert guard.normalize_phone("") == ""


# --------------------------- fake-claim detection ---------------------------


def test_reservation_claim_without_tool_is_fake():
    text = "You're all set for Friday at seven, see you then!"
    assert "reservation_confirmed" in guard.unverified_claims(text, set())


def test_reservation_claim_after_success_is_ok():
    text = "Your reservation is confirmed for Friday at seven."
    assert guard.unverified_claims(text, {"create_reservation"}) == set()


def test_total_claim_without_pricing_tool_is_fake():
    text = "Your total comes to twenty-four dollars."
    assert "order_total" in guard.unverified_claims(text, set())
    assert "order_total" in guard.unverified_claims(text, {"take_message"})


def test_transfer_claim_without_transfer_is_fake():
    text = "Sure, let me connect you to the manager now."
    assert "transfer_started" in guard.unverified_claims(text, set())
    assert guard.unverified_claims(text, {"transfer_call"}) == set()


def test_message_sent_claim_gated_on_take_message():
    text = "I've passed that along to the team."
    assert "message_sent" in guard.unverified_claims(text, set())
    assert guard.unverified_claims(text, {"take_message"}) == set()
    assert guard.unverified_claims(text, {"save_message"}) == set()


def test_innocent_speech_makes_no_claims():
    for text in ("What time would you prefer?",
                 "May I have your name?",
                 "We're open until ten on Fridays.",
                 "I can help with that.",
                 "I'll send that to the team now."):  # intent, not a completed claim
        assert guard.detect_claims(text) == set(), text


# ------------------------------ tool success ------------------------------


def test_tool_success_predicates():
    assert guard.tool_success("create_reservation", {"created": True, "reservation_id": 7})
    assert not guard.tool_success("create_reservation", {"created": False, "reason": "full"})
    assert not guard.tool_success("create_reservation", {"error": "backend 422"})
    assert guard.tool_success("transfer_call", {"transferred": True})
    assert not guard.tool_success("transfer_call", {"transferred": False, "reason": "no number"})
    assert guard.tool_success("take_message", {"created": True, "message_id": 3})
    assert not guard.tool_success("take_message", None)


# ------------------------------ style / recovery ------------------------------


def test_banned_style_flagged():
    for text in ("Awesome, great choice!", "Hey there!", "No problem at all!!",
                 "I'm just an AI so I can't do that.", "Perfect!"):
        assert guard.style_violations(text), text


def test_calm_phrases_pass_style():
    for text in ("Sure.", "I can help with that.", "Let me check that.",
                 "May I have your name?", "I'm sorry, I didn't catch that.",
                 "I can take a message for the manager."):
        assert not guard.style_violations(text), text


def test_clarification_and_stall_detection():
    assert guard.is_clarification("I'm sorry, I didn't catch that — could you repeat it?")
    assert guard.is_clarification("I'm having trouble hearing the order clearly.")
    assert not guard.is_clarification("What time would you prefer?")
    assert guard.is_stall("Let me check that for you.")
    assert guard.MAX_CLARIFY_STRIKES == 2


# --------------------------- prompt safety content ---------------------------

CTX = {
    "business": {
        "name": "Namaste Kitchen", "address": "12 High St", "timezone": "America/New_York",
        "max_party_size": 8, "orders_enabled": False, "phone_forward_to": "",
        "persona_notes": "", "reservation_notes": "", "escalation_rules": "",
    },
    "menu": {}, "kb": [], "hours": [],
}


def _prompt(biz_overrides=None):
    ctx = {**CTX, "business": {**CTX["business"], **(biz_overrides or {})}}
    return build_system_prompt(ctx, datetime(2026, 7, 8, 12, 0))


def test_prompt_one_question_at_a_time():
    p = _prompt()
    assert "ONE QUESTION AT A TIME" in p
    assert "How many people will be in your party?" in p


def test_prompt_tool_gated_phrases():
    p = _prompt()
    assert "TOOL-GATED PHRASES" in p
    assert "created=true" in p
    assert "transfer_call is actually being called" in p


def test_prompt_no_transfer_configured_never_connects():
    p = _prompt({"phone_forward_to": ""})
    assert "transfer is NOT available" in p
    assert "I'm unable to transfer directly right now" in p


def test_prompt_transfer_configured_allows_gated_transfer():
    p = _prompt({"phone_forward_to": "+16650000000"})
    assert "transfer IS configured" in p
    assert "transferred=false" in p


def test_prompt_orders_without_pos_is_order_request():
    p = _prompt({"orders_enabled": False})
    assert "order request" in p
    assert "can't confirm the total or kitchen acceptance" in p
    assert "NEVER state a price, total, or pickup time" in p


def test_prompt_banned_words_and_recovery():
    p = _prompt()
    for phrase in ('"awesome"', '"great choice"', '"hey there"', "I'm just an AI"):
        assert phrase in p, phrase
    assert "After TWO" in p and "failed attempts" in p


def test_prompt_grounding_unknown_answer():
    p = _prompt()
    assert "I don't have that information available" in p
    assert "Never guess" in p


def test_prompt_no_menu_no_invention():
    p = _prompt()
    assert "do NOT name or price any dish" in p
