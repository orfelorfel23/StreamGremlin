# ==============================================================================
# StreamGremlin Hub - Multi-Stage Production Dockerfile
# ==============================================================================
FROM python:3.12-slim AS builder

WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ==============================================================================
# Runtime Stage
# ==============================================================================
FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/orfelorfel23/StreamGremlin"
LABEL org.opencontainers.image.description="StreamGremlin - Interactive Twitch Chat, Overlay & Input Chaos Engine"
LABEL org.opencontainers.image.licenses="CC0-1.0"

# Install age for encrypted secret handling & curl for healthchecks
RUN apt-get update && apt-get install -y --no-install-recommends \
    age \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 1000 appuser

WORKDIR /app

COPY --from=builder /install /usr/local
COPY --chown=appuser:appuser . /app

RUN mkdir -p /app/config /app/sounds /app/tts_cache \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 4736

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:4736/api/health || exit 1

CMD ["uvicorn", "src.hub.main:app", "--host", "0.0.0.0", "--port", "4736"]
