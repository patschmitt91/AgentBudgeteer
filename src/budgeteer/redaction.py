"""Central redaction for logs, span attributes, and persisted state.

The helpers here scrub common secret shapes so they do not surface in:

* `logging` records (via :class:`RedactionFilter`)
* OpenTelemetry span attributes (via :func:`redact_mapping`)
* Ledger rows or any persisted blobs (via :func:`redact`)

Patterns covered:

* ``sk-`` prefixed API keys (OpenAI/Anthropic style)
* Bearer tokens in ``Authorization`` headers
* JSON Web Tokens (three dot-separated base64 segments)
* Long hex strings (40+ characters; catches most opaque keys)
* Values for env vars whose name contains ``KEY``, ``SECRET``, ``TOKEN``,
  ``PASSWORD``, or ``CONNECTION_STRING`` — see :data:`SECRET_ENV_NAMES`
  for the baseline list.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from typing import Any

REDACTED = "[REDACTED]"

SECRET_ENV_NAMES: frozenset[str] = frozenset(
    {
        "AZURE_OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "APPLICATIONINSIGHTS_CONNECTION_STRING",
        "GITHUB_TOKEN",
    }
)

_SECRET_NAME_HINTS = (
    "KEY",
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "CONNECTION_STRING",
)

# Order matters: more specific patterns first so a matched token does not
# get partially re-matched by a broader rule.
_PATTERNS: tuple[re.Pattern[str], ...] = (
    # JWTs: header.payload.signature, each base64url.
    re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b"),
    # sk-... API keys (OpenAI / Anthropic). At least 16 chars after sk-.
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"),
    # Bearer tokens.
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-\.=]{8,}\b"),
    # 40+ char hex blobs (git SHAs are 40 chars; this also catches many
    # opaque keys). This intentionally runs last so it does not eat the
    # tail of an already-matched token.
    re.compile(r"\b[a-fA-F0-9]{40,}\b"),
)


def _scan_env_secret_values() -> tuple[str, ...]:
    """Snapshot env values whose names hint at a secret, for literal masking."""

    out: list[str] = []
    for name, value in os.environ.items():
        if not value:
            continue
        upper = name.upper()
        if upper in SECRET_ENV_NAMES or any(hint in upper for hint in _SECRET_NAME_HINTS):
            out.append(value)
    # Sort longest-first so substrings of larger secrets are masked before
    # smaller ones get a chance to create partial overlaps.
    out.sort(key=len, reverse=True)
    return tuple(out)


# Cached snapshot used on the redact() hot path. Refreshed lazily when the
# env-var count changes, or eagerly via :func:`refresh_env_cache`. See
# harden/phase-3 #3B (the cache exists because ledger writes now flow
# through redact()).
_ENV_SECRET_CACHE: tuple[str, ...] = ()
_ENV_SECRET_CACHE_SIZE: int = -1


def refresh_env_cache() -> None:
    """Re-snapshot secret-bearing env vars; call after env mutation."""
    global _ENV_SECRET_CACHE, _ENV_SECRET_CACHE_SIZE
    _ENV_SECRET_CACHE = _scan_env_secret_values()
    _ENV_SECRET_CACHE_SIZE = len(os.environ)


def _iter_env_secret_values() -> tuple[str, ...]:
    global _ENV_SECRET_CACHE_SIZE
    if len(os.environ) != _ENV_SECRET_CACHE_SIZE:
        refresh_env_cache()
    return _ENV_SECRET_CACHE


def redact(text: str) -> str:
    """Return ``text`` with known secret shapes replaced by :data:`REDACTED`."""

    if not text:
        return text
    out = text
    # Mask env-provided secrets by literal value first. This catches
    # non-pattern secrets (short keys, custom tokens) that would otherwise
    # slip through the regex list.
    for literal in _iter_env_secret_values():
        if literal and literal in out:
            out = out.replace(literal, REDACTED)
    for pat in _PATTERNS:
        out = pat.sub(REDACTED, out)
    return out


def redact_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    """Redact both keys flagged as secret-bearing and any secret-shaped values."""

    clean: dict[str, Any] = {}
    for key, value in data.items():
        upper = str(key).upper()
        if upper in SECRET_ENV_NAMES or any(hint in upper for hint in _SECRET_NAME_HINTS):
            clean[key] = REDACTED
            continue
        if isinstance(value, str):
            clean[key] = redact(value)
        elif isinstance(value, Mapping):
            clean[key] = redact_mapping(value)
        else:
            clean[key] = value
    return clean


class RedactionFilter(logging.Filter):
    """Logging filter that rewrites message and args through :func:`redact`."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Redact the pre-formatted message if one was stored.
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
            elif isinstance(record.args, dict):
                record.args = {
                    k: (redact(v) if isinstance(v, str) else v) for k, v in record.args.items()
                }
        return True
