#!/usr/bin/env bash
# Get the code onto GitHub so Render can build it.
#   bash push-to-github.sh
# Uses the GitHub CLI (gh) if installed; otherwise prints the manual remote steps.
# HARD GUARD: refuses to continue if .env (your secrets) would be committed.
set -euo pipefail
cd "$(dirname "$0")"

REPO="${REPO:-receptionist-voice}"

# Belt-and-suspenders: make sure secrets are ignored.
grep -qxF '.env' .gitignore 2>/dev/null || echo '.env' >> .gitignore

[ -d .git ] || { git init -q; git branch -M main; }
git add -A
git commit -q -m "AI receptionist — hosted stack + deploy config" || echo "(nothing new to commit)"

# ABORT if .env somehow got tracked.
if git ls-files --error-unmatch .env >/dev/null 2>&1; then
  echo "XX  .env is tracked by git — that would leak your keys. Fix with:" >&2
  echo "      git rm --cached .env && git commit -m 'drop .env'" >&2
  exit 1
fi
echo "==> .env is safely ignored."

if command -v gh >/dev/null 2>&1; then
  echo "==> Creating private GitHub repo '$REPO' and pushing"
  gh repo create "$REPO" --private --source=. --remote=origin --push
  echo "==> Done. Repo is on GitHub (private)."
else
  cat <<EOF

Committed locally. GitHub CLI (gh) isn't installed, so finish manually:
  1. Create an EMPTY repo at https://github.com/new  named "$REPO"
  2. Then run:
       git remote add origin https://github.com/<your-username>/$REPO.git
       git push -u origin main
EOF
fi
