"""Agent tools: schemas (what the LLM sees) + handlers (HTTP to control plane).

Design rules from the platform spec:
- <500ms round-trip budget per tool; control plane is local/co-located.
- Handlers NEVER raise into the pipeline: every failure returns a structured
  error the LLM can speak around ("I'm having trouble with the booking system,
  let me take a message instead").
"""
import hashlib
import json
import os

import httpx
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.services.llm_service import FunctionCallParams

CONTROL_PLANE = os.getenv("CONTROL_PLANE_URL", "http://localhost:8080")

# ------------------------------ schemas ------------------------------

check_availability_schema = FunctionSchema(
    name="check_availability",
    description=(
        "Check whether a table is available before booking. Returns availability "
        "and, when full, up to two alternative times to offer the caller."
    ),
    properties={
        "date": {"type": "string", "description": "Requested date, YYYY-MM-DD"},
        "time": {"type": "string", "description": "Requested time, 24h HH:MM"},
        "party_size": {"type": "integer", "description": "Number of guests"},
    },
    required=["date", "time", "party_size"],
)

create_reservation_schema = FunctionSchema(
    name="create_reservation",
    description=(
        "Create a confirmed reservation. Call ONLY after check_availability said "
        "available AND the caller has verbally confirmed the read-back of all details."
    ),
    properties={
        "date": {"type": "string", "description": "YYYY-MM-DD"},
        "time": {"type": "string", "description": "24h HH:MM"},
        "party_size": {"type": "integer"},
        "guest_name": {"type": "string"},
        "guest_phone": {"type": "string", "description": "Caller mobile, digits only"},
        "notes": {"type": "string", "description": "Special requests, optional"},
    },
    required=["date", "time", "party_size", "guest_name", "guest_phone"],
)

take_message_schema = FunctionSchema(
    name="take_message",
    description=(
        "Record a message for the owner/staff, who receive it by SMS immediately. "
        "Use for anything you cannot handle, complaints, large parties, private "
        "events, or when the caller asks for a callback."
    ),
    properties={
        "caller_name": {"type": "string"},
        "caller_phone": {"type": "string"},
        "reason": {"type": "string", "description": "Concise reason + any details"},
        "urgency": {"type": "string", "enum": ["normal", "urgent"]},
    },
    required=["caller_name", "caller_phone", "reason"],
)

create_order_schema = FunctionSchema(
    name="create_order",
    description=(
        "Place a pickup order. Call ONLY after reading back the full order "
        "(items, quantities, total) and the caller has confirmed. Item names "
        "must come from the MENU section of your instructions — never invent "
        "items. If the result has unknown_items, apologize, re-check the menu "
        "with the caller, and try again with corrected names."
    ),
    properties={
        "guest_name": {"type": "string"},
        "guest_phone": {"type": "string", "description": "Caller mobile for pickup contact, digits only"},
        "items": {
            "type": "array",
            "description": "Order lines",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "EXACT menu item name"},
                    "qty": {"type": "integer"},
                    "notes": {"type": "string", "description": "e.g. 'no onion'"},
                },
                "required": ["name", "qty"],
            },
        },
        "notes": {"type": "string", "description": "Whole-order notes, optional"},
    },
    required=["guest_name", "guest_phone", "items"],
)

tools = ToolsSchema(standard_tools=[
    check_availability_schema,
    create_reservation_schema,
    create_order_schema,
    take_message_schema,
])

# ------------------------------ handlers ------------------------------


def _clean_phone(raw: str) -> str:
    """Keep digits (and a leading +). The caller may say a formatted number and
    the STT returns "(665) 493-1454"; store it clean instead of rejecting."""
    if not raw:
        return raw
    digits = "".join(ch for ch in raw if ch.isdigit())
    return ("+" + digits) if raw.strip().startswith("+") else digits


def _idem_key(call_id: str, tool: str, args: dict) -> str:
    """Deterministic idempotency key: same call + same tool + same arguments
    = same key, so an LLM retry or duplicated tool call can't double-book.
    Different arguments (caller changed the time) get a fresh key."""
    blob = json.dumps(args, sort_keys=True, default=str)
    return hashlib.sha1(f"{call_id}|{tool}|{blob}".encode()).hexdigest()


def make_handlers(slug: str, call_id: str):
    """Handlers close over the tenant slug + call id (multi-tenant safe).
    Every mutating call carries the tenant (slug in the URL path) AND an
    idempotency_key — the two hard safety rules for tool calls."""
    client = httpx.AsyncClient(base_url=CONTROL_PLANE, timeout=5.0)

    async def _post(path: str, payload: dict) -> dict:
        try:
            r = await client.post(path, json=payload)
            return r.json() if r.status_code == 200 else {"error": f"backend {r.status_code}"}
        except Exception as e:  # noqa: BLE001 — must never raise into the pipeline
            return {"error": f"backend unreachable: {type(e).__name__}"}

    async def check_availability(params: FunctionCallParams):
        result = await _post(f"/agent/{slug}/availability", {
            "date": params.arguments["date"],
            "time": params.arguments["time"],
            "party_size": params.arguments["party_size"],
        })
        await params.result_callback(result)

    async def create_reservation(params: FunctionCallParams):
        result = await _post(f"/agent/{slug}/reservations", {
            **params.arguments,
            "guest_phone": _clean_phone(params.arguments.get("guest_phone", "")),
            "notes": params.arguments.get("notes", ""),
            "call_id": call_id,
            "idempotency_key": _idem_key(call_id, "create_reservation", params.arguments),
        })
        await params.result_callback(result)

    async def create_order(params: FunctionCallParams):
        result = await _post(f"/agent/{slug}/orders", {
            **params.arguments,
            "guest_phone": _clean_phone(params.arguments.get("guest_phone", "")),
            "notes": params.arguments.get("notes", ""),
            "call_id": call_id,
            "idempotency_key": _idem_key(call_id, "create_order", params.arguments),
        })
        await params.result_callback(result)

    async def take_message(params: FunctionCallParams):
        result = await _post(f"/agent/{slug}/messages", {
            **params.arguments,
            "caller_phone": _clean_phone(params.arguments.get("caller_phone", "")),
            "urgency": params.arguments.get("urgency", "normal"),
            "call_id": call_id,
            "idempotency_key": _idem_key(call_id, "take_message", params.arguments),
        })
        await params.result_callback(result)

    async def report_call(
        outcome: str, summary: str, transcript: str,
        caller_phone: str = "", called_number: str = "", language: str = "",
    ):
        """Not an LLM tool — called by the pipeline at call end."""
        await _post(f"/agent/{slug}/calls", {
            "call_id": call_id, "caller_phone": caller_phone,
            "called_number": called_number, "language": language,
            "outcome": outcome, "summary": summary, "transcript": transcript,
        })
        await client.aclose()

    return {
        "check_availability": check_availability,
        "create_reservation": create_reservation,
        "create_order": create_order,
        "take_message": take_message,
        "_report_call": report_call,
    }
