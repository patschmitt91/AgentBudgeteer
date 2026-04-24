# ADR-0002: Use SQLite as the Fleet coordination ledger

- **Status:** Accepted
- **Date:** 2026-04-24
- **Deciders:** @patschmitt91
- **Supersedes:** —

## Context

The Fleet strategy fans out N workers, each with its own git worktree,
each claiming a shard of files to edit. The workers need to coordinate:

- Claim a shard exactly once.
- Record per-shard status (pending, claimed, finished, failed).
- Record per-shard token and cost accounting for budget attribution.
- Let a single process aggregate results at the end.

Candidate backing stores:

1. SQLite file in `.budgeteer/`.
2. An in-memory `multiprocessing.Manager` dict.
3. A network store (Redis, PostgreSQL, Azure Table Storage).

## Decision

Use a single SQLite file (`.budgeteer/fleet.db` by default, configurable
via `fleet.sqlite_path` in policy.yaml). One file per run. Workers open
the DB with WAL mode; claims use an atomic `UPDATE … WHERE status =
'pending'` guarded by `RETURNING`.

## Consequences

### Positive

- **Zero deployment surface.** No service to stand up, no connection
  string to configure, no network failure mode.
- **Atomic claim semantics.** SQLite's WAL mode and conditional UPDATE
  give us exactly-once shard claims without a distributed lock.
- **Inspectable after the fact.** A failed run leaves the ledger on
  disk; `sqlite3 .budgeteer/fleet.db` works for forensics without
  special tooling.
- **Tests run without a server.** `tests/test_fleet.py` runs against a
  temp-directory SQLite, not a mocked service client.

### Negative

- **Single-host only.** Workers must share a filesystem. Cross-host
  fleets would need a network store.
- **Write throughput ceiling.** SQLite serializes writers. For the
  small N (v0 caps `max_workers` at 4) this is not a bottleneck, but
  it is a hard ceiling.
- **Schema migrations are manual.** No Alembic-class tool ships with
  the project. We version the schema inline.

### Neutral

- The cost accounting rows written here are a subset of the rows
  written by the telemetry span exporter. The two sources exist side
  by side on purpose: telemetry is best-effort, the ledger is the
  source of truth for budget enforcement.

## Alternatives considered

### `multiprocessing.Manager` dict

**Rejected.** Evaporates on process crash, so a crashed fleet loses
all shard status. Cannot be inspected post-mortem.

### Redis / PostgreSQL

**Rejected for v0.** Adds an operational dependency that a user has to
stand up before they can try the tool. SQLite is Python stdlib. We can
revisit once cross-host fleets are a real requirement; the code path
that touches the ledger is narrow enough to swap behind a
`LedgerBackend` interface.

### Azure Table Storage

**Rejected for v0.** Same concern as Redis, plus it couples a generic
router to a specific cloud. We do want Azure OpenAI as an adapter; we
do not want Azure as a control-plane dependency.

## Validation

- `tests/test_fleet.py` exercises claim, finish, and failure paths.
- Schema is applied idempotently on every ledger open so fresh runs
  work without a manual migration step.
