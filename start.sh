#!/usr/bin/env bash
# Boot both planes in one container.
#   1. Control plane (FastAPI)  -> 127.0.0.1:8080  (internal only; the agent
#      calls it over loopback via CONTROL_PLANE_URL)
#   2. Voice agent (Twilio)     -> 0.0.0.0:$PORT   (public; Twilio hits the
#      TwiML webhook at POST / and the media stream at wss://$PUBLIC_HOST/ws)
set -euo pipefail

PORT="${PORT:-7860}"

# Public hostname for Twilio's TwiML. Fly: set in fly.toml. Render: provided
# automatically as RENDER_EXTERNAL_HOSTNAME. Fall back to it if PUBLIC_HOST unset.
PUBLIC_HOST="${PUBLIC_HOST:-${RENDER_EXTERNAL_HOSTNAME:-}}"
if [[ -z "$PUBLIC_HOST" ]]; then
  echo "FATAL: no PUBLIC_HOST / RENDER_EXTERNAL_HOSTNAME set. Twilio TwiML needs it." >&2
  exit 1
fi

# Seed the business. Idempotent ("already seeded"); required on hosts with an
# ephemeral filesystem (e.g. Render free tier) where the DB doesn't persist.
echo "==> Seeding business data (idempotent)"
python seed.py || true

echo "==> Starting control plane on 0.0.0.0:8080 (reachable by the Caddy container)"
uvicorn api.main:app --host 0.0.0.0 --port 8080 &
CONTROL_PID=$!

# If the control plane dies, take the container down so Fly restarts cleanly.
trap 'kill "$CONTROL_PID" 2>/dev/null || true' EXIT

# Wait for the control plane to accept connections before the agent needs it.
for i in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:8080/docs" >/dev/null 2>&1; then
    echo "==> Control plane is up"
    break
  fi
  sleep 0.5
done

echo "==> Starting voice agent (Twilio) on 0.0.0.0:${PORT}, proxy=${PUBLIC_HOST}"
exec python -m agent.bot --transport twilio --host 0.0.0.0 --port "${PORT}" --proxy "${PUBLIC_HOST}"
