#!/usr/bin/env bash
# One-command production deploy to Fly.io (hosted stack).
#
#   Prereqs (do these ONCE, they need a browser + card):
#     brew install flyctl
#     fly auth login          # sign in, add a credit card
#
#   Then:
#     bash deploy-fly.sh
#
# Reads secrets from .env, creates the app + volume, ships the container, points
# your Twilio number at the Fly host, and seeds the business. Re-runnable: after
# the first time it just redeploys.
set -uo pipefail
cd "$(dirname "$0")"

say()  { printf "\n\033[1;36m==> %s\033[0m\n" "$*"; }
warn() { printf "\n\033[1;33m!!  %s\033[0m\n" "$*"; }
die()  { printf "\n\033[1;31mXX  %s\033[0m\n" "$*" >&2; exit 1; }

APP_NAME="${APP_NAME:-receptionist-voice}"
REGION="${REGION:-iad}"

# --- prereqs -----------------------------------------------------------------
command -v fly >/dev/null 2>&1 || die "flyctl not installed →  brew install flyctl   then  fly auth login"
fly auth whoami >/dev/null 2>&1 || die "Not logged in →  fly auth login   (sign in + add a card)"
[ -f .env ] || die ".env not found."
set -a; source .env; set +a
for v in DEEPGRAM_API_KEY OPENAI_API_KEY CARTESIA_API_KEY CARTESIA_VOICE_ID \
         TWILIO_ACCOUNT_SID TWILIO_AUTH_TOKEN TWILIO_FROM_NUMBER; do
  [ -n "${!v:-}" ] || die "$v is empty in .env"
done

# --- app ---------------------------------------------------------------------
if fly apps list 2>/dev/null | grep -qw "$APP_NAME"; then
  say "App '$APP_NAME' already exists"
else
  say "Creating app '$APP_NAME'"
  fly apps create "$APP_NAME" \
    || die "Couldn't create '$APP_NAME' (name may be taken). Re-run as:  APP_NAME=your-unique-name bash deploy-fly.sh"
fi
HOST="$APP_NAME.fly.dev"

# Keep fly.toml's app + PUBLIC_HOST in sync with the real app name.
python3 - "$APP_NAME" "$HOST" <<'PY'
import re, sys
app, host = sys.argv[1], sys.argv[2]
t = open("fly.toml").read()
t = re.sub(r'(?m)^app\s*=.*$', f'app = "{app}"', t)
t = re.sub(r'(?m)^(\s*)PUBLIC_HOST\s*=.*$', rf'\1PUBLIC_HOST = "{host}"', t)
open("fly.toml", "w").write(t)
print("fly.toml → app:", app, "| PUBLIC_HOST:", host)
PY

# --- volume (persistent SQLite) ----------------------------------------------
if fly volumes list -a "$APP_NAME" 2>/dev/null | grep -qw data; then
  say "Volume 'data' already exists"
else
  say "Creating 1GB volume 'data'"
  fly volumes create data --size 1 --region "$REGION" --yes -a "$APP_NAME"
fi

# --- secrets -----------------------------------------------------------------
MONITOR_TOKEN="${MONITOR_TOKEN:-}"
[ -n "$MONITOR_TOKEN" ] || MONITOR_TOKEN=$(openssl rand -hex 16)
say "Setting secrets"
fly secrets set -a "$APP_NAME" \
  DEEPGRAM_API_KEY="$DEEPGRAM_API_KEY" \
  OPENAI_API_KEY="$OPENAI_API_KEY" \
  OPENAI_MODEL="${OPENAI_MODEL:-gpt-4o-mini}" \
  CARTESIA_API_KEY="$CARTESIA_API_KEY" \
  CARTESIA_VOICE_ID="$CARTESIA_VOICE_ID" \
  TWILIO_ACCOUNT_SID="$TWILIO_ACCOUNT_SID" \
  TWILIO_AUTH_TOKEN="$TWILIO_AUTH_TOKEN" \
  TWILIO_FROM_NUMBER="$TWILIO_FROM_NUMBER" \
  MONITOR_TOKEN="$MONITOR_TOKEN" >/dev/null

# --- deploy (single machine, no HA duplicate) --------------------------------
say "Deploying — first build pulls the voice stack, can take a few minutes"
fly deploy -a "$APP_NAME" --ha=false || die "deploy failed — check the build output above"

# --- health ------------------------------------------------------------------
say "Health check"
for _ in $(seq 1 20); do
  curl -fsS "https://$HOST/status" >/dev/null 2>&1 && { curl -s "https://$HOST/status"; echo; break; }
  sleep 3
done

# --- Twilio webhook ----------------------------------------------------------
say "Pointing Twilio $TWILIO_FROM_NUMBER at https://$HOST/"
NUM_ENC=$(python3 -c "import urllib.parse,os;print(urllib.parse.quote(os.environ['TWILIO_FROM_NUMBER']))")
LOOKUP=$(curl -s -u "$TWILIO_ACCOUNT_SID:$TWILIO_AUTH_TOKEN" \
  "https://api.twilio.com/2010-04-01/Accounts/$TWILIO_ACCOUNT_SID/IncomingPhoneNumbers.json?PhoneNumber=$NUM_ENC")
NUM_SID=$(python3 -c "import sys,json
try: d=json.loads(sys.argv[1])
except Exception: print(''); sys.exit()
ns=d.get('incoming_phone_numbers',[])
print(ns[0]['sid'] if ns else '')" "$LOOKUP")
if [ -n "$NUM_SID" ]; then
  curl -s -X POST -u "$TWILIO_ACCOUNT_SID:$TWILIO_AUTH_TOKEN" \
    "https://api.twilio.com/2010-04-01/Accounts/$TWILIO_ACCOUNT_SID/IncomingPhoneNumbers/$NUM_SID.json" \
    --data-urlencode "VoiceUrl=https://$HOST/" --data-urlencode "VoiceMethod=POST" >/dev/null \
    && say "Twilio webhook set → https://$HOST/" \
    || warn "Twilio update failed — set Voice webhook to https://$HOST/ manually."
else
  warn "Couldn't find $TWILIO_FROM_NUMBER on the account — set Voice webhook to https://$HOST/ manually."
fi

# --- seed the business (once, on the volume) ---------------------------------
say "Seeding business data"
fly ssh console -a "$APP_NAME" -C "python seed.py" \
  || warn "Seed didn't run (machine not ready yet?). Run later:  fly ssh console -a $APP_NAME -C 'python seed.py'"

cat <<EOF

  ┌────────────────────────────────────────────────────────────┐
   LIVE ON FLY.  Call:  $TWILIO_FROM_NUMBER
   Monitor:  https://$HOST/monitor?token=$MONITOR_TOKEN
   Logs:     fly logs -a $APP_NAME
   Status:   fly status -a $APP_NAME
   Save this MONITOR_TOKEN: $MONITOR_TOKEN
  └────────────────────────────────────────────────────────────┘
EOF
