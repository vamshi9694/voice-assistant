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

from .prompts import DAYS, LANG_RULES

CONTROL_PLANE = os.getenv("CONTROL_PLANE_URL", "http://127.0.0.1:8080")


async def _post(path: str, payload: dict) -> dict:
    try:
        async with httpx.AsyncClient(base_url=CONTROL_PLANE, timeout=5.0) as c:
            r = await c.post(path, json=payload)
            return r.json() if r.status_code == 200 else {"error": f"backend {r.status_code}"}
    except Exception as e:  # noqa: BLE001 — must never raise into the pipeline
        return {"error": f"backend unreachable: {type(e).__name__}"}


def _clean_phone(raw: str) -> str:
    if not raw:
        return raw
    digits = "".join(ch for ch in raw if ch.isdigit())
    return ("+" + digits) if raw.strip().startswith("+") else digits


def build_initial_node(ctx: dict, slug: str, call_id: str, language: str, now: datetime) -> dict:
    """Return the greeting NodeConfig. Later nodes are returned by the direct
    functions as they transition. `ctx` is the business context bundle."""
    biz = ctx["business"]
    max_party = biz.get("max_party_size", 8)
    kb_lines = "\n".join(f"- {k['topic']}: {k['answer']}" for k in ctx["kb"]) or "- (none)"
    hours_lines = "\n".join(
        f"- {DAYS[h['day']]} ({h['name']}): opens {h['opens']}, last seating {h['last_seating']}, closes {h['closes']}"
        for h in sorted(ctx["hours"], key=lambda x: (x["day"], x["opens"]))
    ) or "- (no hours configured)"
    lang_rule = LANG_RULES.get(language, LANG_RULES["en"])

    # Persona + knowledge as the persistent role message (carries across nodes).
    role = f"""You are the phone receptionist for {biz['name']}, {biz['address']}. \
You're a professional, friendly receptionist on a live phone call — polished and \
efficient, warm but businesslike, NEVER flirtatious, gushing, or overly familiar \
(no pet names, measured enthusiasm). Use contractions, keep replies to one short \
sentence, ask ONE thing at a time. Read phone numbers back digit by digit.
{lang_rule}

WHAT YOU HANDLE — and what you don't:
- You can book reservations, answer questions from the knowledge below, and take \
messages. That's it.
- You do NOT take food, drink, or takeaway/delivery orders over the phone. If a \
caller tries to order food, warmly explain you can't take orders by phone, and \
offer to book them a table or take a message instead. Never walk a caller through \
a food order.
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
            res = await _post(f"/agent/{slug}/messages", {
                "caller_name": caller_name, "caller_phone": _clean_phone(caller_phone),
                "reason": reason, "urgency": "urgent" if urgent else "normal", "call_id": call_id,
            })
            return res, node_done()

        return {
            "name": "take_message",
            "task_messages": [{"role": "system", "content":
                "Take a message. Collect the caller's name, phone number, and the reason — ONE at a "
                "time. Read the phone number back digit by digit to confirm, then call save_message."}],
            "functions": [save_message],
        }

    def node_confirm() -> dict:
        async def confirm_booking(flow_manager):
            """The caller confirmed all details are correct. Create the reservation now."""
            s = flow_manager.state
            res = await _post(f"/agent/{slug}/reservations", {
                "date": s.get("date"), "time": s.get("time"), "party_size": s.get("party_size"),
                "guest_name": s.get("name"), "guest_phone": _clean_phone(s.get("phone", "")),
                "call_id": call_id,
            })
            return res, node_done()

        async def change_details(flow_manager):
            """The caller wants to change the date, time, or party size."""
            return {"ok": True}, node_collect_details()

        return {
            "name": "confirm",
            "task_messages": [{"role": "system", "content":
                "Read back ALL details in one sentence — name, party size, date, time, and the phone "
                "number digit by digit — and ask them to confirm. If they confirm, call "
                "confirm_booking. If they want to change something, call change_details."}],
            "functions": [confirm_booking, change_details],
        }

    def node_collect_contact() -> dict:
        async def set_contact(flow_manager, name: str, phone: str):
            """Store the caller's name and phone, then move to confirmation.

            Args:
                name: The caller's name.
                phone: The caller's phone number, any format.
            """
            flow_manager.state["name"] = name
            flow_manager.state["phone"] = phone
            return {"ok": True}, node_confirm()

        return {
            "name": "collect_contact",
            "task_messages": [{"role": "system", "content":
                "Ask for the caller's name and mobile number, ONE at a time. A US phone number "
                "has 10 digits — if you've collected fewer than 10, don't proceed: tell them how "
                "many you have and ask for the rest. Read the full number back digit by digit to "
                "confirm. Only when name + all 10 digits are confirmed, call set_contact."}],
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
                f"Collect party size, date, and time — ONE at a time. Parties over {max_party} "
                "can't be booked here, so if it's larger you'll switch to taking a message. When you "
                "have all three, call check_table. If it comes back unavailable with alternatives, "
                "offer two of them and ask which suits."}],
            "functions": [check_table],
        }

    def node_greeting() -> dict:
        async def start_booking(flow_manager):
            """The caller wants to book, reserve, or get a table."""
            return {"ok": True}, node_collect_details()

        async def start_message(flow_manager):
            """The caller wants to leave a message, speak to a manager, has a complaint, a private
            event, catering, or anything you can't handle yourself."""
            return {"ok": True}, node_message()

        return {
            "name": "greeting",
            "role_message": role,
            "task_messages": [{"role": "system", "content":
                "Figure out what the caller wants. Answer questions from the knowledge above "
                "directly (no tools). If they want to book a table, call start_booking. If they want "
                "to leave a message, a manager, or raise something you can't handle, call "
                "start_message. If they try to ORDER FOOD or takeaway, do NOT take the order — warmly "
                "say you can't take orders by phone and offer to book a table or take a message."}],
            "functions": [start_booking, start_message],
            "respond_immediately": False,  # we already greeted via TTS; wait for the caller
        }

    return node_greeting()
