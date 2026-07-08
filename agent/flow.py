"""Booking flow as a STATE MACHINE (Pipecat Flows) — the reliability upgrade.

Instead of one big system prompt (which suffers "context drift" as the call
grows), each stage is a node with its own focused instructions and only the
tools it needs. The model can't wander: it's greeting → collect details →
check availability → collect contact → confirm → book, with a message-taking
branch. State (the collected slots) lives in flow_manager.state.

Enabled by FLOWS=on. The single-prompt path in pipeline.py stays the default
and the instant fallback.

STATUS: v1 — needs live-call iteration. If anything misbehaves, set FLOWS=off.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any

import httpx
from loguru import logger

from .guard import normalize_phone, phone_problem
from .prompts import DAYS, LANG_RULES

CONTROL_PLANE = os.getenv("CONTROL_PLANE_URL", "http://127.0.0.1:8080")


async def _post(path: str, payload: dict) -> dict:
    try:
        async with httpx.AsyncClient(base_url=CONTROL_PLANE, timeout=5.0) as c:
            r = await c.post(path, json=payload)
            return r.json() if r.status_code == 200 else {"error": f"backend {r.status_code}"}
    except Exception as e:  # noqa: BLE001 — must never raise into the pipeline
        return {"error": f"backend unreachable: {type(e).__name__}"}


def _phone_gate(raw: str) -> dict | None:
    """HARD GATE: no booking or message leaves a node with a bad callback
    number — the flow stays in place and the LLM must re-ask."""
    problem = phone_problem(raw)
    if problem is None:
        return None
    return {"created": False, "error": "invalid_phone", "problem": problem,
            "instruction": "Ask for the full phone number again; do not proceed."}


# Shared per-node recovery rule: never loop the same question forever.
RECOVERY = (
    " If you can't understand an answer, re-ask ONCE, phrased differently. After a "
    "second failure on the same item, STOP re-asking: summarize what you have so far "
    "and offer to send it as a message so the team can call back."
)


def build_initial_node(ctx: dict, slug: str, call_id: str, language: str, now: datetime) -> dict:
    """Return the greeting NodeConfig. Later nodes are returned by the direct
    functions as they transition. `ctx` is the business context bundle."""
    biz = ctx["business"]
    max_party = biz.get("max_party_size", 8)
    can_transfer = bool((biz.get("phone_forward_to") or "").strip())
    kb_lines = "\n".join(f"- {k['topic']}: {k['answer']}" for k in ctx["kb"]) or "- (none)"
    hours_lines = "\n".join(
        f"- {DAYS[h['day']]} ({h['name']}): opens {h['opens']}, last seating {h['last_seating']}, closes {h['closes']}"
        for h in sorted(ctx["hours"], key=lambda x: (x["day"], x["opens"]))
    ) or "- (no hours configured)"
    lang_rule = LANG_RULES.get(language, LANG_RULES["en"])

    # Persona + knowledge as the persistent role message (carries across nodes).
    role = f"""You are the phone receptionist for {biz['name']}, {biz['address']}. \
You're a calm, experienced front-desk person on a live phone call — short, polite, \
confident, never robotic or repetitive, NEVER flirtatious, gushing, or overly \
familiar. Use contractions, keep replies to one short sentence, ask ONE thing at a \
time (never bundle "date, time and party size" into one question). Read phone \
numbers back digit by digit. Prefer calm phrases: "Sure.", "I can help with that.", \
"May I have your name?", "I'm sorry, I didn't catch that." NEVER say: "awesome", \
"perfect", "great choice", "hey there", "no problem at all", "I'm just an AI".
Never claim something is confirmed, booked, sent, or transferred unless the matching \
tool on this call succeeded.
{lang_rule}

WHAT YOU HANDLE — and what you don't:
- You can book reservations, answer questions from the knowledge below, take \
messages, and pass along order requests. That's it.
- There is NO live order system on this line. You may write an order down as an \
ORDER REQUEST for the team, but you must never state a price, total, pickup time, \
or kitchen acceptance. Say: "I can send this as an order request to the restaurant \
team, but I can't confirm the total or kitchen acceptance by phone."
- Parties larger than {max_party} can't be booked online — take a message for the \
manager instead.
- Allergy/dietary questions: answer ONLY from the knowledge below; if it's not \
covered, say you'd rather not guess and offer a callback.
- Never invent menu items, prices, or availability.
- NEVER call a tool with information the caller hasn't EXPLICITLY said on this call. \
Never invent or assume a name, phone, date, time, or party size — if a field is \
missing, ASK for it. A booking with made-up details is a serious failure.

BUSINESS KNOWLEDGE (answer questions from this directly, no tools):
{kb_lines}

OPENING HOURS:
{hours_lines}

Right now it is {now.strftime('%A %d %B %Y, %H:%M')} ({biz.get('timezone','')}).
You have already greeted the caller out loud — do not greet again."""

    # ----------------------------- nodes ------------------------------------
    def node_done() -> dict:
        return {
            "name": "done",
            "task_messages": [{"role": "system", "content":
                "Warmly confirm what was done and say a friendly goodbye. Do not start a new task."}],
            "functions": [],
        }

    def node_message() -> dict:
        async def save_message(flow_manager, caller_name: str, caller_phone: str,
                               reason: str, urgent: bool = False):
            """Record the message for the team. Call once you have the caller's name, phone, and reason.

            Args:
                caller_name: The caller's name.
                caller_phone: The caller's phone number, any format.
                reason: Short reason / what they want.
                urgent: True for complaints, time-sensitive matters, or if they ask for a manager.
            """
            gate = _phone_gate(caller_phone)
            if gate:
                return gate, None  # stay in this node until the phone is valid
            res = await _post(f"/agent/{slug}/messages", {
                "caller_name": caller_name, "caller_phone": normalize_phone(caller_phone),
                "reason": reason, "urgency": "urgent" if urgent else "normal", "call_id": call_id,
            })
            # Only a real success ends the task — a failure must never sound sent.
            if res.get("created"):
                return res, node_done()
            return res, None

        return {
            "name": "take_message",
            "task_messages": [{"role": "system", "content":
                "Take a message. Collect the caller's name, phone number, and the reason — ONE at a "
                "time. Read the phone number back digit by digit to confirm, then call save_message. "
                "Say the message was sent ONLY after save_message succeeds; if it fails, apologize "
                "and say the team may not get the message, don't pretend otherwise." + RECOVERY}],
            "functions": [save_message],
        }

    def node_manager() -> dict:
        """Manager/human request — deterministic: transfer only when configured."""
        async def transfer_to_manager(flow_manager):
            """Transfer the live call to the restaurant's staff line. The caller asked for a person."""
            res = await _post(f"/agent/{slug}/transfer", {
                "call_id": call_id, "reason": "caller asked for manager/human"})
            if res.get("transferred"):
                return res, node_done()
            return res, node_message()  # fall back to a message, honestly

        async def leave_manager_message(flow_manager):
            """The caller agreed to leave a message for the manager instead."""
            return {"ok": True}, node_message()

        if can_transfer:
            task = ("The caller wants a manager/human. Say \"Sure, let me connect you.\" and "
                    "call transfer_to_manager IMMEDIATELY. If it comes back transferred=false, "
                    "say you weren't able to transfer and offer to take a message instead — "
                    "never pretend the transfer happened.")
            fns = [transfer_to_manager, leave_manager_message]
        else:
            task = ("The caller wants a manager/human, but NO transfer is available on this "
                    "line. Say exactly this, then proceed: \"I'm unable to transfer directly "
                    "right now, but I can take a message for the manager.\" NEVER say "
                    "\"let me connect you\" or imply a transfer. If they agree, call "
                    "leave_manager_message; mark the message urgent.")
            fns = [leave_manager_message]
        return {"name": "manager", "task_messages": [{"role": "system", "content": task}],
                "functions": fns}

    def node_order_request() -> dict:
        """Order attempt without a POS — capture as an order-request message.
        The bot must never invent prices, totals, pickup times, or availability."""
        async def send_order_request(flow_manager, caller_name: str, caller_phone: str,
                                     items: str):
            """Send the collected items to the team as an order REQUEST (not a confirmed order).

            Args:
                caller_name: The caller's name.
                caller_phone: The caller's phone number, any format.
                items: The items and quantities exactly as the caller stated them.
            """
            gate = _phone_gate(caller_phone)
            if gate:
                return gate, None
            res = await _post(f"/agent/{slug}/messages", {
                "caller_name": caller_name, "caller_phone": normalize_phone(caller_phone),
                "reason": f"ORDER REQUEST (unconfirmed): {items}",
                "urgency": "normal", "call_id": call_id,
            })
            if res.get("created"):
                return res, node_done()
            return res, None

        return {
            "name": "order_request",
            "task_messages": [{"role": "system", "content":
                "The caller wants to place a food order. There is NO order system, so first say: "
                "\"I can send this as an order request to the restaurant team, but I can't "
                "confirm the total or kitchen acceptance by phone.\" If they want to go ahead, "
                "collect the items ONE at a time, then their name, then their phone number. "
                "NEVER state a price, total, or pickup time. Read the items and phone back, "
                "then call send_order_request. Only after it succeeds, say the request was "
                "passed to the team." + RECOVERY}],
            "functions": [send_order_request],
        }

    def node_confirm() -> dict:
        async def confirm_booking(flow_manager):
            """The caller confirmed all details are correct. Create the reservation now."""
            s = flow_manager.state
            # HARD GATE: every slot must exist and the phone must be valid —
            # the backend is the brain, the LLM only phrases the outcome.
            missing = [k for k in ("date", "time", "party_size", "name") if not s.get(k)]
            if missing:
                return {"created": False, "error": f"missing: {', '.join(missing)}",
                        "instruction": "Ask the caller for the missing detail."}, None
            gate = _phone_gate(s.get("phone", ""))
            if gate:
                return gate, None
            res = await _post(f"/agent/{slug}/reservations", {
                "date": s.get("date"), "time": s.get("time"), "party_size": s.get("party_size"),
                "guest_name": s.get("name"), "guest_phone": normalize_phone(s.get("phone", "")),
                "call_id": call_id,
            })
            if res.get("created"):
                return res, node_done()
            return res, None  # stay: speak the real reason, never claim it's booked

        async def change_details(flow_manager):
            """The caller wants to change the date, time, or party size."""
            return {"ok": True}, node_collect_details()

        return {
            "name": "confirm",
            "task_messages": [{"role": "system", "content":
                "Read back ALL details in one sentence — name, party size, date, time, and the phone "
                "number digit by digit — and ask them to confirm. If they confirm, call "
                "confirm_booking. If they want to change something, call change_details. Say the "
                "reservation is confirmed ONLY after confirm_booking returns created=true; if it "
                "fails, explain honestly and offer to take a message instead."}],
            "functions": [confirm_booking, change_details],
        }

    def node_collect_contact() -> dict:
        async def set_contact(flow_manager, name: str, phone: str):
            """Store the caller's name and phone, then move to confirmation.

            Args:
                name: The caller's name.
                phone: The caller's phone number, any format.
            """
            gate = _phone_gate(phone)
            if gate:
                return gate, None  # deterministic: bad number never advances the flow
            flow_manager.state["name"] = name
            flow_manager.state["phone"] = phone
            return {"ok": True}, node_confirm()

        return {
            "name": "collect_contact",
            "task_messages": [{"role": "system", "content":
                "Ask for the caller's name, then their mobile number — ONE question at a time "
                "(\"May I have your name?\" then \"May I have your phone number?\"). A US phone "
                "number has 10 digits — if you've collected fewer, tell them how many you have and "
                "ask for the rest. Read the full number back digit by digit to confirm. Only when "
                "name + all digits are confirmed, call set_contact." + RECOVERY}],
            "functions": [set_contact],
        }

    def node_collect_details() -> dict:
        async def check_table(flow_manager, date: str, time: str, party_size: int):
            """Check whether a table is available. Call once you have date, time, and party size.

            Args:
                date: Requested date as YYYY-MM-DD.
                time: Requested time in 24h HH:MM.
                party_size: Number of guests.
            """
            if party_size > max_party:
                return {"too_large": True, "max": max_party}, node_message()
            res = await _post(f"/agent/{slug}/availability", {
                "date": date, "time": time, "party_size": party_size})
            if res.get("available"):
                flow_manager.state.update(date=date, time=time, party_size=party_size)
                return res, node_collect_contact()
            return res, None  # not available: stay here, offer the alternatives it returned

        return {
            "name": "collect_details",
            "task_messages": [{"role": "system", "content":
                f"Collect party size, date, and time — ONE question at a time (\"How many people "
                f"will be in your party?\" then \"What date would you like?\" then \"What time "
                f"would you prefer?\"). If a date is ambiguous (\"Friday\", \"the 12th\"), confirm "
                f"the exact date before proceeding. Parties over {max_party} can't be booked here, "
                "so if it's larger you'll switch to taking a message. When you have all three, "
                "call check_table. If it comes back unavailable with alternatives, offer two of "
                "them and ask which suits." + RECOVERY}],
            "functions": [check_table],
        }

    def node_greeting() -> dict:
        async def start_booking(flow_manager):
            """The caller wants to book, reserve, or get a table."""
            return {"ok": True}, node_collect_details()

        async def start_message(flow_manager):
            """The caller wants to leave a message, has a complaint, a private
            event, catering, or anything you can't handle yourself."""
            return {"ok": True}, node_message()

        async def ask_for_manager(flow_manager):
            """The caller asks for a manager, owner, or a real person/human."""
            return {"ok": True}, node_manager()

        async def start_order(flow_manager):
            """The caller wants to order food, takeaway, or delivery."""
            return {"ok": True}, node_order_request()

        return {
            "name": "greeting",
            "role_message": role,
            "task_messages": [{"role": "system", "content":
                "Intent routing — figure out what the caller wants, then hand off to exactly one "
                "path. Answer menu/price/hours/address questions from the knowledge above directly "
                "(no tools); if the answer isn't in the knowledge, say \"I don't have that "
                "information available, but I can take a message for the team.\" — never guess. "
                "Booking a table -> start_booking. Manager/owner/human -> ask_for_manager. "
                "Food/takeaway order -> start_order. Message, complaint, or anything else you "
                "can't handle -> start_message." + RECOVERY}],
            "functions": [start_booking, start_message, ask_for_manager, start_order],
            "respond_immediately": False,  # we already greeted via TTS; wait for the caller
        }

    return node_greeting()
