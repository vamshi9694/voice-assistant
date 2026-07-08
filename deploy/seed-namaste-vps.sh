#!/usr/bin/env bash
# Seed Namaste into the LIVE VPS database and re-point the demo number,
# without rebuilding the image. Run from the repo root on your Mac:
#
#     bash deploy/seed-namaste-vps.sh
#
# Override the host if needed:  VPS=root@45.32.217.30 bash deploy/seed-namaste-vps.sh
set -euo pipefail
cd "$(dirname "$0")/.."

VPS="${VPS:-root@45.32.217.30}"

echo "==> copying seed_namaste.py to $VPS"
scp seed_namaste.py "$VPS:/tmp/seed_namaste.py"

echo "==> running the seed inside the app container"
ssh "$VPS" 'bash -s' <<'REMOTE'
set -euo pipefail
# Find the receptionist app container (it publishes/exposes 7860 + 8080).
CID=$(docker ps --format '{{.ID}} {{.Image}}' | grep -iE 'app|receptionist' | awk '{print $1}' | head -1)
[ -z "$CID" ] && CID=$(docker ps --format '{{.ID}}' | head -1)
echo "app container: $CID"
docker cp /tmp/seed_namaste.py "$CID:/app/seed_namaste.py"
docker exec "$CID" python seed_namaste.py
REMOTE

echo "==> done. Call +14063568133 — it should now answer as Namaste."
