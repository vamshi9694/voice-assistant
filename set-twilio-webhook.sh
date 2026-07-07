#!/usr/bin/env bash
# Point a Twilio number's voice webhook at the tenant-routing endpoint (/voice).
#   bash set-twilio-webhook.sh https://receptionist-voice.onrender.com [NUMBER]
# NUMBER defaults to TWILIO_FROM_NUMBER in .env — pass each tenant number to
# route several numbers at the same host (the /voice webhook resolves tenant
# by the called number).
set -euo pipefail
cd "$(dirname "$0")"

URL="${1:-}"
[ -n "$URL" ] || { echo "Usage: bash set-twilio-webhook.sh https://your-host [+E164]"; exit 1; }
URL="${URL%/}/voice"   # always target the tenant-routing TwiML endpoint
NUMBER_ARG="${2:-}"

set -a; source .env; set +a
: "${TWILIO_ACCOUNT_SID:?missing in .env}" "${TWILIO_AUTH_TOKEN:?}"
TARGET_NUMBER="${NUMBER_ARG:-${TWILIO_FROM_NUMBER:?set TWILIO_FROM_NUMBER or pass a number}}"

NUM_ENC=$(TARGET_NUMBER="$TARGET_NUMBER" python3 -c "import urllib.parse,os;print(urllib.parse.quote(os.environ['TARGET_NUMBER']))")
SID=$(curl -s -u "$TWILIO_ACCOUNT_SID:$TWILIO_AUTH_TOKEN" \
  "https://api.twilio.com/2010-04-01/Accounts/$TWILIO_ACCOUNT_SID/IncomingPhoneNumbers.json?PhoneNumber=$NUM_ENC" \
  | python3 -c "import sys,json
try: d=json.load(sys.stdin)
except Exception: print(''); sys.exit()
ns=d.get('incoming_phone_numbers',[]); print(ns[0]['sid'] if ns else '')")

[ -n "$SID" ] || { echo "Couldn't find $TARGET_NUMBER on this account."; exit 1; }

curl -s -X POST -u "$TWILIO_ACCOUNT_SID:$TWILIO_AUTH_TOKEN" \
  "https://api.twilio.com/2010-04-01/Accounts/$TWILIO_ACCOUNT_SID/IncomingPhoneNumbers/$SID.json" \
  --data-urlencode "VoiceUrl=$URL" --data-urlencode "VoiceMethod=POST" >/dev/null \
  && echo "Twilio voice webhook set → $URL" \
  || { echo "Twilio update failed."; exit 1; }
