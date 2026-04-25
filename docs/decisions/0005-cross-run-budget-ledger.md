# ADR 0005 — Cross-run rolling-window budget cap (`PersistentBudgetLedger`)

* Status: Accepted
* Date: 2026-04-25
* Deciders: AgentBudgeteer maintainers
* Depends on: agentcore ADR 0003 (`PersistentBudgetLedger`)
* Sibling: PCIV ADR 0007 (same primitive, mirrored wiring)

## Context

`budgeteer run` enforces a per-run hard cap (`--budget`) through an
in-memory `BudgetGovernor`. The governor resets every process. An
operator running 100 sequential `budgeteer run` invocations against a
$2.50 per-run cap can spend $250 in a day while every individual run
respects its $2.50 ceiling.

`RESEARCH.md` flagged this as the most credibility-damaging missing
control: AgentBudgeteer sells itself as a budget-aware runtime
router and couldn't bound spend across runs.

## Decision

Wire `agentcore.budget.PersistentBudgetLedger` (ADR 0003 in
agentcore) into AgentBudgeteer at CLI entry, mirroring the wiring
PCIV ADR 0007 ships.

### Configuration

A new optional `[cross_run]` block in `config/policy.yaml`:

```yaml
cross_run:
  cap_usd: 50.00                    # null/omitted → cross-run cap disabled
  window: monthly                   # "monthly" (YYYY-MM, UTC) or "daily" (YYYY-MM-DD, UTC)
  db_path: .budgeteer/cross_run.db  # resolved relative to policy.yaml; defaults to this
```

Defaults: `cap_usd=None`, `window="monthly"`, `db_path=
.budgeteer/cross_run.db` (next to the policy file). The cross-run
check is opt-in; existing policies without the new block keep their
current behaviour (per-run cap only).

Loader: `budgeteer.budget.load_cross_run(path) -> CrossRunBudgetConfig`.

### Storage

A dedicated SQLite file (`.budgeteer/cross_run.db` by default) holds
the `budget_window` table. AgentBudgeteer already ships a separate
fleet ledger (`fleet.db`); the cross-run file is its own database so
operators can inspect, archive, or move it without touching fleet
state.

### Preflight + router integration

`budgeteer run` opens the ledger and:

1. If `remaining_in_current_window() <= 0`, exits with code 2 and
   `"cross-run … cap exhausted: …"` on stderr.
2. Otherwise computes `effective_budget = min(--budget, remaining)`
   and constructs the `Router` with `budget_cap_usd=effective_budget`.

The router's `Policy.route()` already consumes
`self._governor.remaining` as `budget_remaining`. Capping the
governor's hard cap to the cross-run remaining gives the policy
decision tree a window-aware view without a new code path: the
existing tight-budget guard fires when window remaining falls below
`tight_budget_usd`, naturally degrading to the fallback model. This
satisfies the session-prompt requirement that the router "see
`budget_remaining_window`".

### Post-hoc accounting

After `router.run()` returns (success **or** crash) the actual
`outcome.result.cost_usd` is written to the ledger via
`PersistentBudgetLedger.record_spend(amount, note=task[:64])` in a
`finally` block so partial runs still count against the window. The
short task slug provides per-run audit context without leaking the
full prompt.

`record_spend` may raise `BudgetExceeded` if the actual spend
overshot what fit in the window; that is suppressed at the CLI
boundary because surfacing it would mask the run's own exit status.
Operators see the breach via the next run's preflight rejection.

### Emergency override

A new CLI flag `--ignore-cross-run-cap`:

- Skips the preflight check (logs WARNING with the exhausted-cap
  message) and skips the `effective_budget` capping so the operator
  can spend up to `--budget`.
- Records the actual spend via
  `PersistentBudgetLedger.force_record(amount, reason="--ignore-cross-run-cap")`,
  which inserts a row with `forced=1` and never raises.
- The per-run `--budget` cap still applies; the override is scoped to
  cross-run enforcement only.

### Default behaviour preserved

`[cross_run]` omitted (or `cap_usd: null`) skips opening the ledger
entirely. The output payload's `cross_run` key is absent; existing
pipelines see no change.

## Consequences

- AgentBudgeteer's `agentcore` pin bumps from `v0.2.0` to `v0.4.0`
  (skipping `v0.3.0` because the diff scanner shipped in v0.3.0
  isn't consumed by AgentBudgeteer — its router doesn't write
  content directly). CHANGELOG documents the bump.
- `[tool.uv.sources]` declares an editable override for sibling
  `agentcore` checkouts so local dev works before tagged releases
  are pushed. `uv sync` on a fresh clone still resolves from the
  git pin.
- `budgeteer.budget` gains `CrossRunBudgetConfig` and `load_cross_run`.
  Existing imports of `BudgetGovernor`, `load_degradation`, and
  `load_projection_coefficients` are unchanged.
- One new exit-code-2 failure mode for `budgeteer run` (cross-run
  preflight). The existing `--force-strategy` / `--policy` exit-code-2
  branches stay distinct because they exit before any ledger work.
- `tests/test_cross_run_budget.py` covers the realistic two-sequential
  -invocations path (with the second top-up keyed off the seed ledger
  to remove projection-vs-actual sequencing noise from the test), the
  `--ignore-cross-run-cap` override, and the default-disabled path.
- The fleet strategy's per-shard ledger (`fleet.db`, ADR 0002)
  remains untouched; cross-run enforcement happens at the CLI entry
  *before* the strategy executes, so per-shard logic doesn't need to
  know about the window cap. A future ADR may extend cross-run
  enforcement into per-shard preflight.
- Multi-host enforcement is out of scope. Two hosts running
  `budgeteer run` against the same shared filesystem will share the
  cap via SQLite's WAL + `BEGIN IMMEDIATE`; two hosts with
  independent storage will not. Distributed enforcement requires a
  remote ledger fronting `PersistentBudgetLedger`; tracked in the
  roadmap.
