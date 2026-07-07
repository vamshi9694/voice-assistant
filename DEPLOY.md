# Deploy to production (Fly.io — hosted stack)

This ships the receptionist as a real product: a Twilio number answering live
calls, running the **hosted** model stack (Deepgram STT + OpenAI GPT‑4o‑mini +
Cartesia TTS) on an always‑on Fly machine. No Mac, no local models.

```
Caller ──▶ Twilio number ──POST /──▶ Fly app (TwiML) ──wss://…/ws──▶ voice agent
                                                     │
                                        control plane (loopback :8080) ──▶ SQLite (/data)
```

---

## 0. What you need first (accounts + keys)

You have the **Twilio number**. Get the other three keys (each is a 2‑minute signup):

| Service | Env var | Where |
|---|---|---|
| Deepgram (STT) | `DEEPGRAM_API_KEY` | console.deepgram.com |
| OpenAI (LLM) | `OPENAI_API_KEY` | platform.openai.com |
| Cartesia (TTS) | `CARTESIA_API_KEY`, `CARTESIA_VOICE_ID` | play.cartesia.ai (pick a voice, copy its id) |
| Twilio (you have) | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` | console.twilio.com |

Also install the Fly CLI and log in:

```bash
curl -L https://fly.io/install.sh | sh   # macOS/Linux
fly auth login
```

---

## 1. Create the app + volume

Run from inside `receptionist/`:

```bash
# Create the app WITHOUT deploying yet (so we can set secrets first).
fly launch --no-deploy --copy-config --name receptionist-voice --region iad
```

- If you choose a **different app name**, update two things in `fly.toml`:
  `app = "<name>"` and `PUBLIC_HOST = "<name>.fly.dev"`. They must match.

Create the persistent volume the SQLite DB lives on:

```bash
fly volumes create data --size 1 --region iad
```

---

## 2. Set secrets (never commit these)

```bash
fly secrets set \
  DEEPGRAM_API_KEY=dg_xxx \
  OPENAI_API_KEY=sk_xxx \
  OPENAI_MODEL=gpt-4o-mini \
  CARTESIA_API_KEY=ct_xxx \
  CARTESIA_VOICE_ID=<voice-id> \
  TWILIO_ACCOUNT_SID=ACxxx \
  TWILIO_AUTH_TOKEN=xxx \
  TWILIO_FROM_NUMBER=+1XXXXXXXXXX \
  MONITOR_TOKEN=$(openssl rand -hex 16)
```

`STACK`, `SMART_TURN`, `PUBLIC_HOST`, `DATABASE_URL`, `CONTROL_PLANE_URL` are
already set in `fly.toml` — no need to repeat them here. Save the `MONITOR_TOKEN`
value it prints; you need it to open the live monitor.

---

## 3. Deploy

```bash
fly deploy
```

When it finishes, sanity‑check the app is up:

```bash
curl https://receptionist-voice.fly.dev/status
# => {"status":"ready","transports":["twilio"]}
```

---

## 4. Seed the real business

The demo seed is "Luigi's Trattoria" (slug `luigis-carlton`, which `BUSINESS_SLUG`
points at). For a real customer, edit `seed.py` with their name, hours, capacity,
knowledge base, and `owner_mobile`, redeploy, then run the seed **once** against
the production volume:

```bash
fly ssh console -C "python seed.py"
```

(Or keep `luigis-carlton` for your first end‑to‑end test call.)

---

## 5. Point the Twilio number at the app

In the Twilio console → **Phone Numbers → your number → Voice Configuration**:

- **A call comes in** → **Webhook**
- URL: `https://receptionist-voice.fly.dev/` (note the trailing slash)
- HTTP **POST**
- Save.

That webhook returns TwiML telling Twilio to open the media stream at
`wss://receptionist-voice.fly.dev/ws`, which is where the agent picks up.

---

## 6. Test the live call

Call the Twilio number from any phone. You should hear the greeting, and be able
to book a table / ask hours / leave a message. Watch it happen in real time:

- **Live monitor:** `https://receptionist-voice.fly.dev/monitor?token=<MONITOR_TOKEN>`
  (Latency / Transcript / Tools / Logs tabs).
- **Server logs:** `fly logs`

---

## Operations cheatsheet

```bash
fly logs                       # tail live logs
fly status                     # machine health
fly secrets list               # names only (values hidden)
fly deploy                     # ship a new version
fly releases                   # deploy history
fly deploy --image <prev>      # roll back to a previous image
fly ssh console                # shell into the running machine
fly scale count 1              # keep exactly one always-on machine
```

## Known cost/latency levers

- **Latency:** hosted stack removes the local‑LLM bottleneck. If turn‑taking
  feels slow, that's endpointing — Deepgram's is on by default; you can also set
  `SMART_TURN=local` (needs adding `local-smart-turn-v3` to the Dockerfile's
  pipecat extras and bumping RAM to 2gb).
- **Cost:** ~per‑minute Deepgram + OpenAI + Cartesia + Twilio. The local stack in
  `STACK=local` is the margin play for later, once call volume justifies a GPU.

## When to graduate off SQLite

SQLite on a single Fly volume is fine for one pilot business. For multiple
tenants or higher volume, provision Fly Postgres and set `DATABASE_URL` to it —
the code (SQLModel) is already Postgres‑ready; only the URL changes.
