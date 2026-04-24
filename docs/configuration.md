# Configuration

All runtime behavior is driven by [config/policy.yaml](../config/policy.yaml).
This page documents every top-level key and its sub-keys. Environment
variable overrides are listed where they apply.

## `version`

Schema version. Integer. Must be `1` for this release.

## `pricing`

USD per 1M tokens, per model id. Map of `model_id -> {input_per_mtok,
output_per_mtok}`. Values are floats in USD. Used by the budget governor's
projection and by the per-run cost accounting middleware. Override any value
to match your negotiated rates; no default is safe for billing.

## `model_defaults`

Which model id each strategy asks for when the policy does not override it
per task.

| Key                     | Consumed by                                |
|-------------------------|--------------------------------------------|
| `single_agent_primary`  | `SingleAgent` first choice                 |
| `single_agent_fallback` | `SingleAgent` after degradation            |
| `pciv_planner`          | PCIV plan + verify phases                  |
| `pciv_implementer`      | PCIV implement phase                       |
| `fleet_worker`          | Every fleet worker                         |

Environment overrides applied by the adapters:

| Variable                              | Replaces placeholder      |
|---------------------------------------|---------------------------|
| `AZURE_OPENAI_REASONING_DEPLOYMENT`   | `azure-reasoning`         |
| `AZURE_OPENAI_CODEGEN_DEPLOYMENT`     | `azure-codegen`           |
| `ANTHROPIC_PRIMARY_MODEL`             | `anthropic-primary`       |
| `ANTHROPIC_FALLBACK_MODEL`            | `anthropic-fallback`      |

## `pciv.config_path`

Path to the sibling pciv project's `plan.yaml`. Resolved relative to the
policy file unless absolute. The CLI flag `--pciv-config` overrides this.

## `fleet`

| Key                    | Purpose                                              |
|------------------------|------------------------------------------------------|
| `max_workers`          | Cap on concurrent fleet workers (also shard count).  |
| `per_shard_max_tokens` | Target output-token budget per shard for projection. |
| `sqlite_path`          | SQLite ledger path for shard coordination.           |

## `projection`

Output-token projection coefficients consumed by `BudgetGovernor` before
any network call.

| Key                          | Strategy     | Meaning                                 |
|------------------------------|--------------|-----------------------------------------|
| `single_base`                | SingleAgent  | Baseline output tokens                  |
| `single_per_planning_step`   | SingleAgent  | Extra tokens per detected planning step |
| `pciv_multiplier`            | PCIV         | Multiplier over single_base             |
| `pciv_per_planning_step`     | PCIV         | Extra tokens per planning step          |
| `fleet_per_shard`            | Fleet        | Tokens projected per shard              |
| `fleet_max_shards`           | Fleet        | Upper bound on shards regardless of N   |

Tune these against measured run outcomes before trusting the projection.

## `classifier`

Heuristic wordlists. Empty or missing lists fall back to the defaults
compiled into `classifier.py`.

| Key                   | Purpose                                        |
|-----------------------|------------------------------------------------|
| `reasoning_tokens`    | Words that raise reasoning-ratio score.        |
| `mechanical_tokens`   | Words that raise mechanical-ratio score.       |
| `imperative_verbs`    | Verbs that count toward planning-depth score.  |

## `routing`

Decision-tree thresholds for the v0 hand-tuned policy.

| Key                              | Used by the decision tree to                      |
|----------------------------------|---------------------------------------------------|
| `large_context_token_threshold`  | Force SingleAgent when context exceeds this.      |
| `tight_budget_usd`               | Force SingleAgent when budget below this.         |
| `short_latency_seconds`          | Force SingleAgent when latency target below this. |
| `fleet_min_file_count`           | Minimum files before Fleet is considered.         |
| `fleet_max_coupling`             | Maximum coupling before Fleet is considered.      |
| `pciv_min_reasoning_ratio`       | Minimum reasoning ratio for PCIV.                 |
| `pciv_min_planning_depth`        | Minimum planning depth for PCIV.                  |

## `degradation`

Triggered when `projected_cost > trigger_ratio * budget_remaining`.

| Key              | Purpose                                            |
|------------------|----------------------------------------------------|
| `trigger_ratio`  | Float in (0, 1]. Controls when degradation fires.  |
| `swap`           | List of `{from, to, protect_roles}` swap entries.  |

`protect_roles` (e.g. `["planner", "critic"]`) are never degraded; only
implementers / workers are swapped.

## `telemetry`

OpenTelemetry spans emit automatically. If
`APPLICATIONINSIGHTS_CONNECTION_STRING` is set, spans export to Azure
Monitor; otherwise they are dropped by a silent provider. Set
`BUDGETEER_CONSOLE_TRACES=1` to route spans to stdout for local debugging.
The CLI already prints JSON on stdout, so the console exporter is opt-in.
