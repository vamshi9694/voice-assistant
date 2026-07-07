#!/usr/bin/env bash
# setup.sh — one-shot local setup for the AI receptionist MVP on macOS (Apple Silicon).
#
#   bash setup.sh
#
# Idempotent: safe to re-run. Skips work that's already done.
# Doesn't start long-running servers — see the runbook it prints at the end.
set -euo pipefail

# ---------- pretty logging ----------
BOLD=$(tput bold 2>/dev/null || true); DIM=$(tput dim 2>/dev/null || true)
GREEN=$(tput setaf 2 2>/dev/null || true); YELLOW=$(tput setaf 3 2>/dev/null || true)
RED=$(tput setaf 1 2>/dev/null || true); RESET=$(tput sgr0 2>/dev/null || true)
step()  { echo; echo "${BOLD}==>${RESET} ${BOLD}$*${RESET}"; }
ok()    { echo "  ${GREEN}✓${RESET} $*"; }
warn()  { echo "  ${YELLOW}!${RESET} $*"; }
fail()  { echo "  ${RED}✗${RESET} $*" >&2; exit 1; }
info()  { echo "  ${DIM}$*${RESET}"; }

# ---------- 0. sanity ----------
step "0/7  Checking prerequisites"

[[ "$(uname -s)" == "Darwin" ]] || warn "Not macOS — MLX STT is Apple-Silicon only. Set STACK=hosted in .env, or continue for control-plane-only testing."
[[ "$(uname -m)" == "arm64" ]] || warn "Not arm64 — Whisper-MLX won't run. Same fix as above."

# Homebrew
if ! command -v brew >/dev/null 2>&1; then
    fail "Homebrew not found. Install from https://brew.sh then re-run."
fi
ok "Homebrew: $(brew --version | head -1)"

# Python 3.11+ (Pipecat needs it)
PY=""
for cand in python3.12 python3.11 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
        V=$("$cand" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        MAJ=${V%.*}; MIN=${V#*.}
        if (( MAJ == 3 && MIN >= 11 )); then PY="$cand"; break; fi
    fi
done
if [[ -z "$PY" ]]; then
    warn "Python 3.11+ not found — installing python@3.12 via brew..."
    brew install python@3.12
    PY=python3.12
fi
ok "Python: $($PY --version) at $(command -v $PY)"

# ffmpeg (used by faster-whisper for resampling and by kokoro for audio io)
if ! command -v ffmpeg >/dev/null 2>&1; then
    step "0.5/7  Installing ffmpeg (required by audio libs)"
    brew install ffmpeg
fi
ok "ffmpeg: $(ffmpeg -version | head -1 | awk '{print $3}')"

# Ollama (skipped if user chose hosted stack)
STACK_DEFAULT="local"
if ! command -v ollama >/dev/null 2>&1; then
    warn "Ollama not installed — will install now (needed for STACK=local)."
    brew install --cask ollama || brew install ollama
fi
ok "Ollama: $(ollama --version 2>&1 | head -1)"

# ---------- 1. virtualenv ----------
step "1/7  Creating virtualenv (.venv)"
if [[ ! -d .venv ]]; then
    "$PY" -m venv .venv
    ok "created .venv"
else
    ok ".venv already exists"
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip wheel setuptools --quiet
ok "pip $(pip --version | awk '{print $2}')"

# ---------- 2. Python deps ----------
step "2/7  Installing Python dependencies"
info "control plane (fastapi/sqlmodel/httpx/twilio)..."
pip install --quiet -r requirements.txt

info "media plane (pipecat + local stack extras)..."
# Extras breakdown:
#   webrtc               -> browser dev UI (SmallWebRTC transport + aiortc)
#   silero               -> VAD (acoustic layer of turn-taking)
#   local-smart-turn-v3  -> semantic turn detection (ONNX, CPU)
#   whisper              -> faster-whisper (CPU fallback / needed shim)
#   local-smart-turn-v3  -> local semantic turn detection
#   runner               -> the FastAPI dev runner + telephony helpers
pip install --quiet "pipecat-ai[webrtc,silero,whisper,local-smart-turn-v3,runner]"

# MLX Whisper is a separate install (Apple Silicon only, downloads model on first use)
if [[ "$(uname -m)" == "arm64" && "$(uname -s)" == "Darwin" ]]; then
    info "installing mlx-whisper (Apple Silicon STT)..."
    pip install --quiet mlx-whisper
    ok "mlx-whisper installed"
else
    warn "skipping mlx-whisper (not Apple Silicon) — pipeline will fall back to faster-whisper on CPU"
fi

# Kokoro TTS (ONNX runtime, cross-platform)
info "installing kokoro-onnx (local TTS)..."
pip install --quiet kokoro-onnx
ok "kokoro-onnx installed"

ok "all Python deps installed"

# ---------- 3. .env ----------
step "3/7  Environment file"
if [[ ! -f .env ]]; then
    cp .env.example .env
    ok "created .env from .env.example (edit to add Twilio creds later)"
else
    ok ".env exists (leaving it alone)"
fi
# Load STACK for later steps
STACK=$(grep -E '^STACK=' .env | tail -1 | cut -d= -f2 | tr -d '"' || echo "$STACK_DEFAULT")
STACK=${STACK:-$STACK_DEFAULT}
ok "STACK=$STACK"

# ---------- 4. Ollama model ----------
if [[ "$STACK" == "local" ]]; then
    step "4/7  Pulling local LLM (Ollama, Qwen 2.5 14B — ~9GB, one-time)"
    if ! pgrep -x ollama >/dev/null 2>&1; then
        warn "Ollama daemon not running. Start it in another terminal:  ollama serve"
        warn "Skipping model pull for now — run 'ollama pull qwen2.5:14b' after starting the daemon."
    else
        MODEL=$(grep -E '^OLLAMA_MODEL=' .env | tail -1 | cut -d= -f2 | tr -d '"')
        MODEL=${MODEL:-qwen2.5:14b}
        if ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$MODEL"; then
            ok "$MODEL already pulled"
        else
            info "pulling $MODEL (this can take a while over cellular)..."
            ollama pull "$MODEL"
            ok "$MODEL ready"
        fi
    fi
else
    step "4/7  Skipping Ollama (STACK=$STACK — using hosted APIs)"
    warn "Remember to set DEEPGRAM_API_KEY / OPENAI_API_KEY / CARTESIA_API_KEY in .env"
fi

# ---------- 5. seed DB ----------
step "5/7  Seeding the demo restaurant"
python seed.py

# ---------- 6. smoke test the control plane ----------
step "6/7  Smoke-testing the control plane"
python -m uvicorn api.main:app --port 8080 > /tmp/receptionist-api.log 2>&1 &
API_PID=$!
trap 'kill $API_PID 2>/dev/null || true' EXIT
# wait up to 8s for the port
for i in {1..40}; do
    if curl -sf http://localhost:8080/agent/luigis-carlton/context >/dev/null 2>&1; then break; fi
    sleep 0.2
done
if ! curl -sf http://localhost:8080/agent/luigis-carlton/context >/dev/null 2>&1; then
    tail -20 /tmp/receptionist-api.log
    fail "control plane didn't come up — see log above"
fi
ok "control plane responding on :8080"

# One-shot booking flow to prove everything is wired
RESP=$(curl -s -X POST http://localhost:8080/agent/luigis-carlton/reservations \
    -H 'content-type: application/json' \
    -d '{"date":"2026-07-11","time":"19:00","party_size":2,"guest_name":"Smoke Test","guest_phone":"+61400000001","call_id":"setup-smoke"}')
if echo "$RESP" | grep -q '"created":true'; then
    ok "test booking succeeded: $RESP"
else
    warn "test booking response: $RESP"
fi

kill $API_PID 2>/dev/null || true
trap - EXIT

# ---------- 7. done ----------
step "7/7  Setup complete ✨"
cat <<EOF

  ${BOLD}Next: run it in TWO terminals.${RESET}

  ${BOLD}Terminal 1 — control plane (booking API + SMS fallback prints here):${RESET}
    ${DIM}source .venv/bin/activate${RESET}
    ${DIM}uvicorn api.main:app --port 8080 --reload${RESET}

EOF
if [[ "$STACK" == "local" ]]; then
    cat <<EOF
  ${BOLD}Terminal 2 — Ollama (LLM):${RESET}  keep this running
    ${DIM}ollama serve${RESET}

  ${BOLD}Terminal 3 — the agent (browser voice UI):${RESET}
    ${DIM}source .venv/bin/activate${RESET}
    ${DIM}python -m agent.bot${RESET}

EOF
else
    cat <<EOF
  ${BOLD}Terminal 2 — the agent (browser voice UI):${RESET}
    ${DIM}source .venv/bin/activate${RESET}
    ${DIM}python -m agent.bot${RESET}

EOF
fi
cat <<EOF
  Then open ${BOLD}http://localhost:7860${RESET}, click Connect, and try:
     • "Are you open Monday?"           (FAQ — closed Mondays)
     • "Table for 4 Saturday at 7"      (books; try again for alternative times)
     • "I'd like to book a function"    (urgent message → SMS printed in Terminal 1)

  Test end the daily digest any time:
     ${DIM}curl -X POST http://localhost:8080/owner/luigis-carlton/digest${RESET}

  First voice call will be slow (~30-60s) as Whisper + Kokoro download their
  models. Every call after that streams instantly.
EOF
