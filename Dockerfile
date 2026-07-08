# Production image — HOSTED stack only (Deepgram + OpenAI + Cartesia + Twilio).
# Deliberately excludes the local ML stack (MLX/Whisper/Kokoro/Smart-Turn),
# which is Mac-only and multi-GB. Runs both processes: control plane (internal
# :8080) + the Twilio voice agent (public :7860).
FROM python:3.12-slim

# System deps: ffmpeg/libsndfile for audio, curl for the healthcheck,
# build-essential so optional native wheels (pyrnnoise denoiser) can compile.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg libsndfile1 curl ca-certificates build-essential \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=7860 \
    STACK=hosted \
    SMART_TURN=off \
    CONTROL_PLANE_URL=http://127.0.0.1:8080 \
    DATABASE_URL=sqlite:////data/receptionist.db

WORKDIR /app

# Control-plane deps first (best layer caching), then the hosted pipecat extras.
COPY requirements-hosted.txt ./
RUN pip install -r requirements-hosted.txt \
 && pip install "pipecat-ai[webrtc,silero,deepgram,openai,cartesia,groq,runner]==1.5.0" \
 && (pip install pyrnnoise || echo "pyrnnoise unavailable — DENOISE will no-op")

COPY . .
RUN chmod +x start.sh && mkdir -p /data

EXPOSE 7860
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS http://127.0.0.1:${PORT}/status || exit 1

CMD ["./start.sh"]
