# 0004 — Shared core via `agentcore`

## Status

Accepted (2025).

## Context

The redaction module in `src/budgeteer/redaction.py` was a near-byte-identical copy of PCIV's. The Azure OpenAI client we share via the embedded PCIV adapter only supported API-key auth. Phase 4 of `HARDENING_PROMPT.md` calls out that bug fixes (e.g. the env-snapshot cache landed in Phase 3B) had to be ported by hand between repos.

## Decision

Adopt the new sibling repo **`agentcore`** (located alongside `AgentBudgeteer/` and `PCIV/`) as the single source of truth for cross-cutting infrastructure. The first round of extraction covers `agentcore.redaction` and `agentcore.azure_client` (AAD-first). `src/budgeteer/redaction.py` becomes a re-export shim so existing call sites keep working.

## Consequences

- The secret-pattern catalogue lives in one place; future regex additions land in `agentcore` and ship to both projects on the next dependency bump.
- `AgentBudgeteer/pyproject.toml` declares `agentcore>=0.1.0,<0.2`. Development uses an editable install of the local sibling; releases pin the published wheel.
- A breaking change in `agentcore` requires coordinated bumps in PCIV and AgentBudgeteer.
- The Azure adapter wired into the budgeteer-controlled PCIV runs gains AAD support automatically once it switches to `agentcore.azure_client.build_client`.

See `agentcore/docs/decisions/0001-extracted-from-pciv-and-agentbudgeteer.md`.
