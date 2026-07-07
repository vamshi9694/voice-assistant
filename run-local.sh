#!/usr/bin/env bash
# One-command local bring-up for the AI receptionist (hosted stack).
#
#   bash run-local.sh
#
# Does everything: venv, deps, seed, control plane, public tunnel, AND points
# your Twilio number's voice webhook at the tunnel automatically. Ctrl-C stops
# everything cleanly.
set -uo pipefail
cd "$(dirname "$0")"

say()  { printf "\n\033[1;36m==> %s\033[0m\n" "$*"; }
warn() { printf "\n\033[1;33m!!  %s\033[0m\n" "$*"; }
die()  { printf "\n\033[1;31mXX  %s\033[0m\n" "$*" >&2; exit 1; }

# --- 0. venv + env -----------------------------------------------------------
[ -d .venv ] || die "No .venv here. Run:  python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
# shellcheck disable=SC1091
source .venv/bin/activate
[ -f .env ] || die ".env not found."
set -a; source .env; set +a

[ "${STACK:-}" = "hosted" ] || warn "STACK is '${STACK:-unset}', expected 'hosted'."
[ -n "${CARTESIA_VOICE_ID:-}" ] || die "CARTESIA_VOICE_ID is empty in .env — the bot can't speak without it."
[ -n "${TWILIO_ACCOUNT_SID:-}" ] && [ -n "${TWILIO_AUTH_TOKEN:-}" ] || die "Twilio SID/token missing in .env."

# --- 1. deps -----------------------------------------------------------------
say "Checking hosted model libraries"
python -c "import cartesia" 2>/dev/null || { say "Installing Cartesia TTS lib"; pip install -q "pipecat-ai[cartesia]==1.5.0"; }

command -v cloudflared >/dev/null 2>&1 || {
  warn "cloudflared not installed. Install it, then re-run:"
  echo "    brew install cloudflared      (or: https://github.com/cloudflare/cloudflared/releases )"
  die "missing cloudflared"
}

# --- 2. seed (idempotent) ----------------------------------------------------
say "Seeding database"
python seed.py

# --- 3. control plane --------------------------------------------------------
say "Starting control plane on 127.0.0.1:8080"
uvicorn api.main:app --host 127.0.0.1 --port 8080 >/tmp/receptionist-control.log 2>&1 &
CONTROL_PID=$!

for _ in $(seq 1 40); do
  curl -fsS http://127.0.0.1:8080/docs >/dev/null 2>&1 && break
  sleep 0.5
done
curl -fsS http://127.0.0.1:8080/docs >/dev/null 2>&1 || { cat /tmp/receptionist-control.log; die "control plane failed to start"; }

# --- 4. public tunnel --------------------------------------------------------
say "Opening public tunnel to :7860"
cloudflared tunnel --url http://localhost:7860 >/tmp/receptionist-tunnel.log 2>&1 &
TUNNEL_PID=$!

PUBLIC_URL=""
for _ in $(seq 1 40); do
  PUBLIC_URL=$(grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" /tmp/receptionist-tunnel.log | head -1 || true)
  [ -n "$PUBLIC_URL" ] && break
  sleep 1
done
[ -n "$PUBLIC_URL" ] || { cat /tmp/receptionist-tunnel.log; die "tunnel URL not found"; }
PUBLIC_HOST_VAL="${PUBLIC_URL#https://}"
export PUBLIC_HOST="$PUBLIC_HOST_VAL"
say "Public URL: $PUBLIC_URL"

# --- 5. point Twilio at the tunnel (via API) ---------------------------------
say "Configuring Twilio voice webhook for $TWILIO_FROM_NUMBER"
NUM_ENC=$(python -c "import urllib.parse,os;print(urllib.parse.quote(os.environ['TWILIO_FROM_NUMBER']))")
LOOKUP=$(curl -s -u "$TWILIO_ACCOUNT_SID:$TWILIO_AUTH_TOKEN" \
  "https://api.twilio.com/2010-04-01/Accounts/$TWILIO_ACCOUNT_SID/IncomingPhoneNumbers.json?PhoneNumber=$NUM_ENC")
NUM_SID=$(python -c "import sys,json
try: d=json.loads(sys.argv[1])
except Exception: print(''); sys.exit()
ns=d.get('incoming_phone_numbers',[])
print(ns[0]['sid'] if ns else '')" "$LOOKUP")

if [ -n "$NUM_SID" ]; then
  curl -s -X POST -u "$TWILIO_ACCOUNT_SID:$TWILIO_AUTH_TOKEN" \
    "https://api.twilio.com/2010-04-01/Accounts/$TWILIO_ACCOUNT_SID/IncomingPhoneNumbers/$NUM_SID.json" \
    --data-urlencode "VoiceUrl=$PUBLIC_URL/" \
    --data-urlencode "VoiceMethod=POST" >/dev/null \
    && say "Twilio webhook set → ${PUBLIC_URL}/  (POST)" \
    || warn "Twilio update failed — set Voice webhook to ${PUBLIC_URL}/ manually."
else
  warn "Couldn't find $TWILIO_FROM_NUMBER on this Twilio account."
  warn "Set the number's Voice 'A call comes in' webhook to: ${PUBLIC_URL}/  (HTTP POST)"
fi

# --- 6. cleanup on exit ------------------------------------------------------
cleanup() { say "Shutting down"; kill "$CONTROL_PID" "$TUNNEL_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

cat <<EOF

  ┌────────────────────────────────────────────────────────────┐
   READY.  Call your Twilio number:  ${TWILIO_FROM_NUMBER}
   Live monitor:   http://localhost:7860/monitor
   Control logs:   tail -f /tmp/receptionist-control.log
   Press Ctrl-C here to stop everything.
  └────────────────────────────────────────────────────────────┘
EOF

# --- 7. voice agent (foreground) ---------------------------------------------
say "Starting voice agent"
exec python -m agent.bot --transport twilio --host 0.0.0.0 --port 7860 --proxy "$PUBLIC_HOST"
