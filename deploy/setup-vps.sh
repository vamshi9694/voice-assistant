#!/usr/bin/env bash
# Run this ON the Vultr box (Ubuntu 24.04), from inside the repo's deploy/ dir.
# Installs Docker, derives a TLS hostname from the server's public IP via
# sslip.io (no domain needed), and brings up the receptionist behind Caddy.
set -euo pipefail
cd "$(dirname "$0")"

echo "==> Installing Docker (if needed)"
command -v docker >/dev/null 2>&1 || curl -fsSL https://get.docker.com | sh

echo "==> Detecting public IP"
IP=$(curl -fsS https://api.ipify.org)
HOST="${IP}.sslip.io"
echo "PUBLIC_HOST=${HOST}" > .env
echo "    PUBLIC_HOST=${HOST}"

if [ ! -f ../.env ]; then
  echo "XX  ../.env is missing — it must hold your API keys." >&2
  echo "    From your Mac:  scp \"/Users/vamshi/Developer/Ai call/receptionist/.env\" root@${IP}:$(cd .. && pwd)/.env" >&2
  exit 1
fi

echo "==> Building + starting (first build pulls the voice stack; a few minutes)"
docker compose up -d --build

cat <<EOF

  ┌────────────────────────────────────────────────────────────┐
   RUNNING.  Your public host:  https://${HOST}
   1) Point Twilio's voice webhook at:  https://${HOST}/
   2) Health check:   curl https://${HOST}/status
   3) Dashboard:      https://${HOST}/dashboard?token=<DASHBOARD_TOKEN>
   Logs:   docker compose logs -f app
  └────────────────────────────────────────────────────────────┘
EOF
