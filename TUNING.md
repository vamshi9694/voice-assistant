# Tuning notes — what Vapi/Retell do, and how this repo now does it

## The research in one paragraph

Vapi and Retell don't win on a magic model — they constrain it. Vapi Workflows is a
visual builder for "robust, deterministic conversation flows" (nodes + edges); Retell's
Conversation Flow Agents use per-node instructions and logic splits because they're more
predictable than one big prompt. Both prompting guides converge on the same rules: route
intent first, then run one focused playbook; ask ONE question per turn and confirm each
piece before moving on; cap replies at 1–2 sentences; and — most importantly — "the
prompt is probabilistic... for values the model must not be able to fake, use
server-side mechanisms." The backend workflow is the brain; the LLM is only the mouth.

## What changed (spec item → implementation)

| # | Spec item | Where |
|---|---|---|
| 1 | One question at a time | `prompts.py` (bad/good examples verbatim), every node in `flow.py`, checked by `simulate.py --check` |
| 2 | Deterministic workflow | `flow.py` state machine now routes greeting → booking / order request / manager / message / done; backend controls transitions, LLM only phrases them |
| 3 | Tool-gated claims | `prompts.py` TOOL-GATED PHRASES table; nodes only advance on `created=true`/`transferred=true`; `qa.py` fires `fake_claim` when speech outruns tools |
| 4 | Front-desk tone | `prompts.py` + `flow.py` role: calm/short/confident, banned words ("awesome", "perfect", "great choice", "hey there", "no problem at all", "I'm just an AI"), preferred phrases ("Sure.", "May I have your name?") |
| 5 | Confused-caller recovery | 2-strike rule (`guard.MAX_CLARIFY_STRIKES`): re-ask once rephrased, then summarize + offer a message; `RECOVERY` appended to every flow node |
| 6 | Manager escalation | `flow.py node_manager` + `prompts._transfer_rules`: transfer only when `phone_forward_to` is set; otherwise the exact line "I'm unable to transfer directly right now, but I can take a message for the manager." — never a fake transfer |
| 7 | Ordering policy | No POS → ORDER REQUEST via `take_message` with the mandatory "can't confirm the total or kitchen acceptance by phone" line; never invents prices/totals/pickup times |
| 8 | Menu/FAQ grounding | Approved data only; unknown → "I don't have that information available, but I can take a message for the team." |
| 9 | Reservation safety | `guard.valid_phone` HARD GATE in `tools.py` and `flow.py` — an invalid callback number never reaches the backend; all slots + final confirmation required before `create_reservation` |
| 10 | Bad-call detection | `qa.py` new events: `fake_claim`, `stall_no_tool` ("let me check" with no tool), `style_violation`, `clarification_loop`, `caller_distrust` ("you're lying"); existing `hello_retry`/`dead_air` kept |
| 11 | Testing | `tests/test_guard.py` (21 unit tests, no deps: `python -m pytest tests/ -q`) + 14 scripted calls in `scenarios/` |

## The layer that matters: `agent/guard.py`

Pure-Python, prompt-independent enforcement shared by tools, flow, QA, and the
simulator. Two tiers:

1. **Hard gates** — invalid phone / missing slots return a structured error and the
   flow stays put. The model literally cannot book with bad data, no matter what it
   generates.
2. **Detectors** — every bot utterance is scanned for success claims
   ("you're all set", "your total is", "connecting you", "message sent"). A claim with
   no matching successful tool call this call → `fake_claim` QA event. This is the
   alert that catches the bot lying to a caller.

## Running the checks

```bash
# unit tests (offline, instant)
python -m pytest tests/ -q

# scripted scenarios against a live control plane, with acceptance checks:
for s in scenarios/*.txt; do
  OPENAI_API_KEY=sk-... python simulate.py --slug <tenant> --base http://127.0.0.1:8080 \
    --scenario "$s" --check || echo "FAILED: $s"
done
```

`--check` fails the run (exit 1) on: any fake confirmation/total/transfer/"message
sent", an invalid phone accepted by a mutating tool, banned style words, more than one
question in a turn, or a 3rd consecutive clarification re-ask.

## Recommended next step

Turn `FLOWS=on` in staging and run the 14 scenarios over real calls — the state-machine
path is now the closest match to the Vapi/Retell architecture, and per-node instructions
resist context drift far better than the single prompt on long calls.

## Sources

- [Vapi — Workflows overview](https://docs.vapi.ai/workflows/overview)
- [Vapi — Voice AI Prompting Guide](https://docs.vapi.ai/prompting-guide)
- [Retell — Prompt Engineering Guide](https://docs.retellai.com/build/prompt-engineering-guide)
- [Retell — Conversation Flow](https://www.retellai.com/blog/unlocking-complex-interactions-with-retell-ais-conversation-flow)
- [Retell — Prompt-based vs Conversational Pathway](https://www.retellai.com/blog/prompt-based-vs-conversational-pathways-choosing-the-right-approach)
