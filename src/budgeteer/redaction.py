"""Thin re-export of :mod:`agentcore.redaction`.

The redaction module was extracted to the shared ``agentcore`` package in
Phase 4 of ``HARDENING_PROMPT.md``. This shim keeps the existing
``from budgeteer.redaction import ...`` call sites working without churning
the whole codebase. New code should import from ``agentcore.redaction``
directly.

See ``docs/decisions/0011-shared-core-agentcore.md``.
"""

from __future__ import annotations

from agentcore.redaction import (
    REDACTED,
    SECRET_ENV_NAMES,
    RedactionFilter,
    redact,
    redact_mapping,
    refresh_env_cache,
)

__all__ = [
    "REDACTED",
    "SECRET_ENV_NAMES",
    "RedactionFilter",
    "redact",
    "redact_mapping",
    "refresh_env_cache",
]
