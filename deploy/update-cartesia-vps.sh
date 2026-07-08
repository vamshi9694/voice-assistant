#!/usr/bin/env bash
# Push the new Cartesia key to the LIVE VPS and recreate the app container so
# TTS works again. Run from the repo root on your Mac:
#
#     bash deploy/update-cartesia-vps.sh
#
# The key is read from your local .env (CARTESIA_API_KEY=...), so update that
# first (already done) — this just mirrors it to the server.
set -euo pipefail
cd "$(dirname "$0")/.."

VPS="${VPS:-root@45.32.217.30}"
KEY=$(grep -E '^CARTESIA_API_KEY=' .env | head -1 | cut -d= -f2-)
[ -z "$KEY" ] && { echo "No CARTESIA_API_KEY in local .env"; exit 1; }
echo "==> pushing Cartesia key ${KEY:0:12}… to $VPS"

ssh "$VPS" "KEY='$KEY' bash -s" <<'REMOTE'
set -euo pipefail
# Locate the app container and its compose project.
CID=$(docker ps --format '{{.ID}} {{.Image}}' | grep -iE 'app|receptionist' | awk '{print $1}' | head -1)
[ -z "$CID" ] && CID=$(docker ps --format '{{.ID}}' | head -1)
COMPOSE=$(docker inspect "$CID" --format '{{ index .Config.Labels "com.docker.compose.project.config_files" }}')
echo "container: $CID   compose: $COMPOSE"

# env_file is ../.env relative to the compose file (deploy/) = repo-root .env.
ENVFILE="$(dirname "$COMPOSE")/../.env"
[ -f "$ENVFILE" ] || ENVFILE=$(grep -rlE '^CARTESIA_API_KEY=' /root /opt /srv /home 2>/dev/null | head -1)
echo "env file: $ENVFILE"

# Replace or append the key.
if grep -qE '^CARTESIA_API_KEY=' "$ENVFILE"; then
  sed -i "s|^CARTESIA_API_KEY=.*|CARTESIA_API_KEY=$KEY|" "$ENVFILE"
else
  echo "CARTESIA_API_KEY=$KEY" >> "$ENVFILE"
fi
echo "now set: $(grep -E '^CARTESIA_API_KEY=' "$ENVFILE")"

# Recreate the app container so it reloads the env file.
docker compose -f "$COMPOSE" up -d --force-recreate app
echo "recreated. give it ~15s to boot."
REMOTE

echo "==> done. Call the number — you should hear the voice again."
