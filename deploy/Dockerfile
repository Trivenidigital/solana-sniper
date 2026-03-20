# ============================================================================
# Dockerfile for Solana Sniper Trading Bot
# Multi-stage build using uv for fast dependency installation.
#
# Build:  docker build -t solana-sniper -f deploy/Dockerfile .
# Run:    docker run --env-file .env -v ./data:/app/data solana-sniper
# ============================================================================

# ── Stage 1: Build dependencies ────────────────────────────────────────────
FROM python:3.12-slim AS builder

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy dependency files first for layer caching
COPY pyproject.toml ./

# Install dependencies into .venv
RUN uv sync --no-dev --no-install-project

# Copy source code
COPY sniper/ sniper/

# Install the project itself
RUN uv sync --no-dev

# ── Stage 2: Runtime ───────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

RUN groupadd --system sniper && \
    useradd --system --gid sniper --create-home sniper

WORKDIR /app

# Copy the virtual environment from builder
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/sniper /app/sniper
COPY --from=builder /app/pyproject.toml /app/

# Data directory for databases
RUN mkdir -p /app/data && chown -R sniper:sniper /app

# Ensure the venv Python is on PATH
ENV PATH="/app/.venv/bin:$PATH"
ENV SNIPER_DB_PATH="/app/data/sniper.db"
ENV SCOUT_DB_PATH="/app/data/scout.db"

USER sniper

# Default: run sniper in live mode
CMD ["python", "-m", "sniper", "--live"]
