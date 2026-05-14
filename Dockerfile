# syntax=docker/dockerfile:1.7

# ============================================================================
# Stage 1 — Frontend: build the React SPA with bun
# ============================================================================
# bun is the only supported JS runtime / package manager (see AGENTS.md).
# The built artefacts land in ``/out/dist`` and are copied into the Python
# package directory in the next stage so the wheel ships the SPA.
FROM oven/bun:1.2-alpine AS frontend

WORKDIR /ui

# 1. Install JS dependencies in a cached layer keyed on package.json + lockfile.
#    ``bun.lock`` is optional here: the very first Docker build in a fresh
#    checkout will generate it, subsequent builds use ``--frozen-lockfile``
#    once the lockfile has been committed alongside ``package.json``.
COPY ui/package.json ./
COPY ui/bun.lock* ./
RUN --mount=type=cache,target=/root/.bun/install/cache \
    if [ -f bun.lock ]; then \
        bun install --frozen-lockfile; \
    else \
        bun install; \
    fi

# 2. Copy the rest of the frontend source and build.
COPY ui/ ./
RUN bun run build && \
    mkdir -p /out && cp -R ../src/fusionserve/web/dist /out/dist

# ============================================================================
# Stage 2 — Builder: resolve and install Python dependencies system-wide
# ============================================================================
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder

# uv: never download Python (image already has it), always copy files
# (safer across bind-mounts / cross-stage COPY), pre-compile bytecode,
# and install into the system Python instead of a venv.
ENV UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_SYSTEM_PYTHON=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 1. Install dependencies only (no project) in a cached layer.
#    This layer is only invalidated when pyproject.toml / uv.lock change.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv export --frozen --no-dev --no-emit-project --format requirements-txt -o /tmp/requirements.txt \
    && uv pip install --system --no-deps -r /tmp/requirements.txt

# 2. Copy project source and the prebuilt SPA, then install the project
#    itself system-wide. The SPA dist is staged under ``src/fusionserve/web/dist``
#    so uv_build packages it into the wheel automatically.
COPY pyproject.toml uv.lock README.md LICENSE.txt ./
COPY src ./src
COPY --from=frontend /out/dist ./src/fusionserve/web/dist
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system --no-deps .

# ============================================================================
# Stage 3 — Runtime: minimal image with only the installed packages + assets
# ============================================================================
FROM python:3.14-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Create an unprivileged user and group.
RUN groupadd --system --gid 1000 app \
    && useradd --system --uid 1000 --gid app --home /app --shell /usr/sbin/nologin app

WORKDIR /app

# Copy installed packages and console scripts from the builder stage.
# Paths are deterministic for the official python:3.14-slim-bookworm image.
COPY --from=builder /usr/local/lib/python3.14/site-packages /usr/local/lib/python3.14/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy only runtime assets (no sources needed: the package is installed).
COPY --chown=app:app logging.yaml ./

USER app

EXPOSE 8001

# Use exec form so the process receives signals (SIGTERM) correctly.
CMD ["uvicorn", "fusionserve.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8001", \
     "--log-config", "logging.yaml"]
