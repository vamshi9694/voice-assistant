# AI Phone Receptionist — MVP

AI receptionist for restaurants & small businesses: answers every call, books
tables, answers FAQs, takes messages, and texts the owner clean summaries.

Implements **Phase 0/1** of the platform design (`voice-ai-platform-system-design.md`)
for the restaurant vertical (`restaurant-receptionist-product-spec.md`).

## Tech stack (locked)

| Layer | Choice | Why |
|---|---|---|
| Orchestration | **Pipecat 1.5** | Open source; ships the 3-layer turn-taking, barge-in, streaming pipeline |
| VAD | Silero (bundled) | OSS baseline acoustic layer |
| Turn detection | **Smart Turn v3 (local)** | Semantic endpointing — the biggest perceived-latency win |
| STT (local) | Whisper large-v3-turbo via **MLX** | Runs on the M5 Pro GPU, $0 |
| STT (prod/premium) | Deepgram (later: self-hosted Parakeet on GPU) | Streaming, fastest hosted |
| LLM (local) | **Ollama + Qwen 2.5 14B** | Fits in 48GB unified memory, streams fast, good tool-calling |
| LLM (premium) | GPT-4o-mini / Claude | Bring-your-own tier |
| TTS (local) | **Kokoro** (82M, Apache-2.0) | Near-zero marginal cost — the big TTS cost win |
| TTS (premium) | Cartesia | Quality tier |
| Telephony | **Twilio Media Streams** (Phase 1) → Telnyx SIP (Phase 2 margin) | Runner has native support |
| Control plane | **FastAPI + SQLModel** | SQLite dev → Postgres prod, same code |
| SMS | Twilio SMS (console fallback in dev) | Digest + instant alerts |

The `STACK=local|hosted` env var switches the whole model layer — this is the
two-tier strategy from the design docs, in one line of config.

## Repo layout

```
receptionist/
├── agent/                  # MEDIA PLANE (one process per deployment, one task per call)
│   ├── bot.py              #   entry point: browser WebRTC (dev) + Twilio (phone)
│   ├── telephony.py        #   /voice webhook: called number -> tenant routing
│   ├── pipeline.py         #   pipeline factory: STT→LLM→TTS + VAD/SmartTurn + tools
│   ├── tools.py            #   LLM tools (reserve/order/message/search) + idempotency keys
│   ├── qa.py               #   QA observer: latency, dead-air, "hello?", tool failures
│   └── prompts.py          #   system prompt: business context, menu, safety rules
├── api/                    # CONTROL PLANE (normal web backend)
│   ├── main.py             #   /agent/* tool backends + /owner/* + dashboards
│   ├── models.py           #   Business, PhoneNumber, Menu, Order, KB, drafts, users, events
│   ├── auth.py             #   JWT auth: platform_admin / tenant_admin RBAC
│   ├── tenants.py          #   number->tenant resolve, admin CRUD, hours/holidays
│   ├── menu.py             #   menu CRUD + server-validated pickup orders
│   ├── ingest.py           #   menu ingestion: CSV/PDF/image/URL -> draft -> approve
│   ├── crawler.py          #   approved-domain website crawl -> draft -> approve
│   ├── vectorkb.py         #   tenant-scoped vector KB (notes/docs/synced facts)
│   ├── metrics.py          #   call QA metrics ingest + per-tenant summaries
│   ├── idempotency.py      #   server-side dedupe for all mutating tools
│   ├── availability.py     #   capacity engine + alternative-time suggestions
│   ├── notify.py           #   SMS alerts (bookings/orders/messages) + daily digest
│   └── static/             #   client.html (/app), admin.html (/admin-ui)
├── seed.py                 # TWO demo tenants with routed numbers + users
├── verify.py               # end-to-end test suite (python verify.py)
├── requirements.txt
└── .env.example
```

## Multi-tenant call flow

1. Twilio POSTs the call to `https://<voice-host>/voice` (`agent/telephony.py`).
2. The webhook resolves the CALLED number via `GET /agent/resolve?to=+E164`
   and passes `slug`/`to`/`from` into the media stream as parameters.
3. `bot.py` builds the pipeline with only that tenant's config: menu, hours,
   KB, languages (default / enabled / auto-detect / per-language voice),
   reservation + order policy, persona, escalation rules.
4. Every mutating tool call carries the tenant (URL path) + an
   `idempotency_key`; the control plane enforces the safety rules server-side
   (no invented menu items, prices from DB, 10-digit callback numbers,
   large-party threshold, no confirmation without tool success).

## Dashboards

- `/app` — restaurant dashboard (tenant admins): settings, hours + holidays,
  menu + ingestion (CSV/PDF/photo/URL with draft approval), website crawl
  approvals, knowledge base + test search, reservations/orders/messages/calls.
- `/admin-ui` — platform dashboard: tenants, phone numbers, users, per-tenant
  health + call-quality metrics (latency, dead air, "hello?" retries, tool
  failures, duplicates, low-confidence transcripts).
- Seed logins: `admin@platform.local/admin123`, `owner@luigis.local/owner123`,
  `owner@tacos.local/owner123`. Set `AUTH_SECRET` in prod; `AUTH_DISABLED=1`
  for local hacking.

Run the full test suite any time: `python verify.py` (37 checks, no network).

## Quickstart (local, $0 — your M5 Pro)

```bash
# 1. deps
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install "pipecat-ai[webrtc,silero,whisper-mlx,kokoro,local-smart-turn-v3,runner]"

# 2. local LLM
brew install ollama && ollama serve &
ollama pull qwen2.5:14b

# 3. control plane
python seed.py
uvicorn api.main:app --port 8080 &

# 4. agent (serves the WebRTC test UI)
cp .env.example .env
python -m agent.bot
# open http://localhost:7860 → Connect → talk to Luigi's
# open http://localhost:7860/monitor → live log tab (see below)
```

Try: *"Are you open Monday?"* · *"Table for 4 Saturday at 7"* (watch it offer
alternatives when full) · *"Can I speak to the manager about a function?"*
(urgent message → SMS printed to the API console).

### Live monitor (`/monitor`)

`http://localhost:7860/monitor` is a real-time log page served by the agent
process — open it in a second tab while a call runs. It streams four feeds over
SSE (`agent/monitor.py`), each on its own tab:

- **Latency** — per-stage metrics (`enable_metrics=True`): STT/LLM **TTFB**,
  processing time, token counts, TTS chars, and turn-detection time. This is
  where you diagnose where the perceived delay actually is.
- **Transcript** — caller finals + bot responses as they happen.
- **Tools** — each control-plane call (`check_availability`, `create_reservation`,
  `take_message`) with arguments and result.
- **Logs** — the raw loguru stream (connects, disconnects, errors).

Text filter + per-tab counts + follow-tail. Zero extra ports: the routes attach
to the same FastAPI app the runner already serves.

## Phone calls (Phase 1, Twilio)

```bash
ngrok http 7860
python -m agent.bot --transport twilio --proxy YOUR_NGROK_HOST
```
Point your Twilio number's voice webhook at the URL the runner prints (it
serves the TwiML + `/ws` Media Streams endpoint). Fill Twilio creds in `.env`
so SMS confirmations/digests go out for real.

**Owner-side utilities:**
```bash
# edit KB ("KB by SMS" backs onto this endpoint later)
curl -X POST localhost:8080/owner/luigis-carlton/kb \
  -H 'content-type: application/json' \
  -d '{"topic":"good friday","answer":"Closed Good Friday."}'

# send the daily digest now
curl -X POST localhost:8080/owner/luigis-carlton/digest
```

## What's deliberately NOT here yet (per the MVP cut)

Orders/menu/86-list (v1.1) · transfers with whisper context (v1.2) ·
dashboard UI (v1.2) · POS/OpenTable integrations (v2) · payments/PCI (v2) ·
self-hosted Parakeet on cloud GPU (platform Phase 2 — swap inside
`pipeline.build_services()` when call volume justifies the GPU).

## Design notes

- **One call = one PipelineTask.** No shared state across calls; crash = one
  dropped call, never a platform outage.
- **Context-first, tools-second.** Full KB is injected into the system prompt
  at call start (one control-plane round-trip), so FAQ turns cost zero tool
  calls — the fastest possible answer.
- **Tools never raise.** Every handler returns structured errors the LLM can
  speak around and degrade to message-taking. There are no dead ends.
- **Write-time capacity re-check.** Two Friday-7pm callers can race; the
  reservation insert re-validates capacity, and the loser gets alternatives.
- **The digest is the product.** `CallRecord` rows exist so the owner's
  20-second nightly summary is an aggregation, not an afterthought.
