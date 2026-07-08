"""Text harness — test the agent's RESPONSES without phone calls.

Talks to the agent's "brain": the REAL per-tenant system prompt + the REAL
tools, executed against the live control plane (real DB). Lets you iterate on
prompts/menus/rules in seconds instead of dialing over and over.

    # interactive REPL against your VPS:
    OPENAI_API_KEY=sk-... python simulate.py --slug luigis-carlton \
        --base https://45.32.217.30.sslip.io

    # run a scripted scenario (one caller line per row) and print the transcript:
    python simulate.py --slug tacos-el-rey --base http://127.0.0.1:8080 \
        --scenario scenarios/booking.txt

What it exercises: system prompt, tool selection, data capture, availability /
booking / order / message / KB logic, and the server-side safety rules. What it
does NOT: audio, endpointing/pauses, TTS voice, latency (those need a real call).
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

import httpx

from agent import guard
from agent.prompts import build_system_prompt

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
C = {"you": "\033[33m", "bot": "\033[36m", "tool": "\033[35m", "dim": "\033[2m", "off": "\033[0m"}


# OpenAI-format mirror of agent/tools.py (kept in sync by hand).
TOOLS = [
    {"type": "function", "function": {
        "name": "check_availability",
        "description": "Check whether a table is available before booking. Returns availability and up to two alternatives when full.",
        "parameters": {"type": "object", "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD"},
            "time": {"type": "string", "description": "24h HH:MM"},
            "party_size": {"type": "integer"}},
            "required": ["date", "time", "party_size"]}}},
    {"type": "function", "function": {
        "name": "create_reservation",
        "description": "Create a confirmed reservation. Only after availability=true AND the caller confirmed the read-back.",
        "parameters": {"type": "object", "properties": {
            "date": {"type": "string"}, "time": {"type": "string"}, "party_size": {"type": "integer"},
            "guest_name": {"type": "string"}, "guest_phone": {"type": "string"}, "notes": {"type": "string"}},
            "required": ["date", "time", "party_size", "guest_name", "guest_phone"]}}},
    {"type": "function", "function": {
        "name": "take_message",
        "description": "Record a message for staff. Use for anything you can't handle, complaints, large parties, callbacks.",
        "parameters": {"type": "object", "properties": {
            "caller_name": {"type": "string"}, "caller_phone": {"type": "string"},
            "reason": {"type": "string"}, "urgency": {"type": "string", "enum": ["normal", "urgent"]}},
            "required": ["caller_name", "caller_phone", "reason"]}}},
    {"type": "function", "function": {
        "name": "create_order",
        "description": "Place a pickup order after reading back items+total and the caller confirmed. Item names must come from the menu.",
        "parameters": {"type": "object", "properties": {
            "guest_name": {"type": "string"}, "guest_phone": {"type": "string"},
            "items": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"}, "qty": {"type": "integer"}, "notes": {"type": "string"}},
                "required": ["name", "qty"]}},
            "notes": {"type": "string"}},
            "required": ["guest_name", "guest_phone", "items"]}}},
    {"type": "function", "function": {
        "name": "search_knowledge",
        "description": "Search the business KB for a question not covered by your instructions. Answer only from what it returns.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "transfer_call",
        "description": "Transfer the caller to a human. Only if they ask for a person/manager or you can't help. Say you're connecting them first.",
        "parameters": {"type": "object", "properties": {
            "reason": {"type": "string"}}, "required": ["reason"]}}},
]

TOOL_ENDPOINT = {
    "check_availability": "availability",
    "create_reservation": "reservations",
    "take_message": "messages",
    "create_order": "orders",
    "search_knowledge": "kb/search",
    "transfer_call": "transfer",
}


def openai_chat(messages: list) -> dict:
    r = httpx.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
        json={"model": OPENAI_MODEL, "messages": messages, "tools": TOOLS, "temperature": 0.4},
        timeout=40.0,
    )
    if r.status_code != 200:
        sys.exit(f"OpenAI error {r.status_code}: {r.text[:300]}")
    return r.json()["choices"][0]["message"]


def exec_tool(base: str, slug: str, name: str, args: dict) -> dict:
    try:
        r = httpx.post(f"{base}/agent/{slug}/{TOOL_ENDPOINT[name]}", json=args, timeout=15.0)
        try:
            return r.json()
        except Exception:
            return {"error": f"{r.status_code}: {r.text[:150]}"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"control plane unreachable: {type(e).__name__}"}


class CallChecker:
    """Applies the acceptance criteria (agent/guard.py) to a simulated call:
    fake claims, invalid phones sent to tools, style violations, clarification
    loops, multi-question turns. Exit code 1 when any scenario check fails."""

    def __init__(self):
        self.tools_ok: set[str] = set()
        self.clarify_strikes = 0
        self.failures: list[str] = []

    def on_tool(self, name: str, args: dict, result: dict):
        phone = args.get("guest_phone") or args.get("caller_phone")
        if name in ("create_reservation", "create_order", "take_message") and phone is not None:
            if not guard.valid_phone(phone) and (result.get("created") or result.get("ok")):
                self.failures.append(f"invalid phone accepted by {name}: {phone!r}")
        if guard.tool_success(name, result):
            self.tools_ok.add(name)

    def on_bot(self, text: str):
        for kind in guard.unverified_claims(text, self.tools_ok):
            self.failures.append(f"FAKE CLAIM ({kind}): {text[:120]!r}")
        for phrase in guard.style_violations(text):
            self.failures.append(f"banned style {phrase!r}: {text[:100]!r}")
        if text.count("?") > 1:
            self.failures.append(f"multiple questions in one turn: {text[:120]!r}")
        if guard.is_clarification(text):
            self.clarify_strikes += 1
            if self.clarify_strikes > guard.MAX_CLARIFY_STRIKES:
                self.failures.append(f"clarification loop (>{guard.MAX_CLARIFY_STRIKES}): {text[:100]!r}")
        else:
            self.clarify_strikes = 0

    def report(self) -> bool:
        if self.failures:
            print(f"\n{C['you']}CHECKS FAILED:{C['off']}")
            for f in self.failures:
                print(f"  ✗ {f}")
            return False
        print(f"\n{C['bot']}all acceptance checks passed{C['off']}")
        return True


def run_turn(base: str, slug: str, messages: list, user_text: str,
             checker: CallChecker | None = None) -> None:
    messages.append({"role": "user", "content": user_text})
    for _ in range(6):  # allow a few tool rounds per turn
        msg = openai_chat(messages)
        messages.append(msg)
        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            text = msg.get("content", "") or ""
            print(f"{C['bot']}bot>{C['off']} {text}")
            if checker:
                checker.on_bot(text)
            return
        for tc in tool_calls:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except Exception:
                args = {}
            print(f"   {C['tool']}→ {name}({json.dumps(args)}){C['off']}")
            result = exec_tool(base, slug, name, args)
            print(f"   {C['tool']}← {json.dumps(result)}{C['off']}")
            if checker and isinstance(result, dict):
                checker.on_tool(name, args, result)
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(result)})
    print(f"{C['dim']}(stopped after 6 tool rounds){C['off']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", default="luigis-carlton")
    ap.add_argument("--base", default=os.getenv("BASE", "http://127.0.0.1:8080"))
    ap.add_argument("--scenario", help="file of caller lines (one per row; # = comment)")
    ap.add_argument("--check", action="store_true",
                    help="apply guard.py acceptance criteria; exit 1 on any failure")
    a = ap.parse_args()

    if not os.getenv("OPENAI_API_KEY"):
        sys.exit("Set OPENAI_API_KEY (the LLM runs the conversation).")

    try:
        ctx = httpx.get(f"{a.base}/agent/{a.slug}/context", timeout=15.0).json()
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Couldn't fetch context from {a.base} for '{a.slug}': {e}")
    if "business" not in ctx:
        sys.exit(f"Bad context for '{a.slug}': {json.dumps(ctx)[:200]}")

    language = (ctx.get("languages") or {}).get("default", "en")
    system = build_system_prompt(ctx, datetime.datetime.now(), language=language)
    messages = [{"role": "system", "content": system}]

    print(f"{C['dim']}— simulating '{a.slug}' via {a.base} (model {OPENAI_MODEL}, lang {language}) —{C['off']}")
    print(f"{C['bot']}bot>{C['off']} (greeting is spoken on real calls; start typing the caller's side)")

    if a.scenario:
        checker = CallChecker() if a.check else None
        with open(a.scenario) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                print(f"{C['you']}you>{C['off']} {line}")
                run_turn(a.base, a.slug, messages, line, checker=checker)
        if checker and not checker.report():
            sys.exit(1)
    else:
        while True:
            try:
                user = input(f"{C['you']}you>{C['off']} ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if user in ("quit", "exit", ":q"):
                break
            if user:
                run_turn(a.base, a.slug, messages, user)


if __name__ == "__main__":
    main()
