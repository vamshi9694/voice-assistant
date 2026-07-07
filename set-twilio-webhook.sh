#!/usr/bin/env bash
# Point your Twilio number's voice webhook at any host.
#   bash set-twilio-webhook.sh https://receptionist-voice.onrender.com/
# Reads Twilio creds + number from .env. Reusable for Render, Fly, ngrok, etc.
set -euo pipefail
cd "$(dirname "$0")"

URL="${1:-}"
[ -n "$URL" ] || { echo "Usage: bash set-twilio-webhook.sh https://your-host/"; exit 1; }
[[ "$URL" == */ ]] || URL="$URL/"   # Twilio wants the trailing slash

set -a; source .env; set +a
: "${TWILIO_ACCOUNT_SID:?missing in .env}" "${TWILIO_AUTH_TOKEN:?}" "${TWILIO_FROM_NUMBER:?}"

NUM_ENC=$(python3 -c "import urllib.parse,os;print(urllib.parse.quote(os.environ['TWILIO_FROM_NUMBER']))")
SID=$(curl -s -u "$TWILIO_ACCOUNT_SID:$TWILIO_AUTH_TOKEN" \
  "https://api.twilio.com/2010-04-01/Accounts/$TWILIO_ACCOUNT_SID/IncomingPhoneNumbers.json?PhoneNumber=$NUM_ENC" \
  | python3 -c "import sys,json
try: d=json.load(sys.stdin)
except Exception: print(''); sys.exit()
ns=d.get('incoming_phone_numbers',[]); print(ns[0]['sid'] if ns else '')")

[ -n "$SID" ] || { echo "Couldn't find $TWILIO_FROM_NUMBER on this account."; exit 1; }

curl -s -X POST -u "$TWILIO_ACCOUNT_SID:$TWILIO_AUTH_TOKEN" \
  "https://api.twilio.com/2010-04-01/Accounts/$TWILIO_ACCOUNT_SID/IncomingPhoneNumbers/$SID.json" \
  --data-urlencode "VoiceUrl=$URL" --data-urlencode "VoiceMethod=POST" >/dev/null \
  && echo "Twilio voice webhook set → $URL" \
  || { echo "Twilio update failed."; exit 1; }
