# GRX10 Automated Market Research Tool — API container.
# Optional: Render's native Python runtime (render.yaml) is the primary deploy
# path; this Dockerfile is for `runtime: docker`, local parity, or other hosts.
#
# Build context MUST be the repo root (so backend/, connectors/, methods/,
# pipeline/ all copy in as top-level importable packages):
#   docker build -f backend/Dockerfile -t grx10-mr-api .
#   docker run -p 8000:8000 --env-file .env grx10-mr-api

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# psycopg[binary] ships its own libpq, so no system libpq-dev is required;
# curl is handy for in-container health checks.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first for layer caching.
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copy the source packages (repo root build context).
COPY backend ./backend
COPY connectors ./connectors
COPY methods ./methods
COPY pipeline ./pipeline
COPY config ./config
COPY db ./db

# Non-root runtime user.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# $PORT is honoured on platforms that inject it; defaults to 8000 locally.
CMD ["sh", "-c", "uvicorn backend.app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
