# syntax=docker/dockerfile:1.7

# TODO(harden/phase-0): pin python:3.12-slim by digest once Dependabot's
# docker ecosystem update lands (.github/dependabot.yml). Floating tags
# are still a supply-chain risk despite the minor pin.

# ---- builder ----
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

# Install uv from the upstream distroless image (patch-pinned).
# Bump via Dependabot; track ghcr.io/astral-sh/uv releases.
COPY --from=ghcr.io/astral-sh/uv:0.4.30 /uv /usr/local/bin/uv

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

ARG VERSION=dev

LABEL org.opencontainers.image.title="agent-budgeteer" \
      org.opencontainers.image.source="https://github.com/patschmitt91/AgentBudgeteer" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.description="Cost-aware routing layer for agent strategies on Microsoft Agent Framework."

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

# HEALTHCHECK uses `version` (no env required) instead of `doctor` so containers
# without API keys are not permanently unhealthy. Use `budgeteer doctor` from
# orchestration when a deeper probe is wanted.
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD ["budgeteer", "version"]

ENTRYPOINT ["budgeteer"]
CMD ["--help"]
