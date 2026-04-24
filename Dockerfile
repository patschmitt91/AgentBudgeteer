# syntax=docker/dockerfile:1.7

# ---- builder ----
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

# Install uv from the upstream distroless image (pinned minor).
COPY --from=ghcr.io/astral-sh/uv:0.4 /uv /usr/local/bin/uv

# git is required because `pciv` is pulled via a PEP 508 direct reference.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy only dependency metadata first to maximize layer cache hits.
COPY pyproject.toml uv.lock README.md ./

# Source is required for the editable install of the local package.
COPY src ./src
COPY config ./config

RUN uv sync --no-dev --frozen

# ---- runtime ----
FROM python:3.12-slim AS runtime

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LOG_FORMAT=json

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1001 budgeteer \
    && useradd --system --uid 1001 --gid 1001 --no-create-home --shell /usr/sbin/nologin budgeteer

WORKDIR /app

COPY --from=builder --chown=budgeteer:budgeteer /app /app

USER budgeteer

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD ["budgeteer", "doctor"]

ENTRYPOINT ["budgeteer"]
CMD ["--help"]
