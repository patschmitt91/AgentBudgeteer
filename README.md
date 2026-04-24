# agent-budgeteer

Status: **alpha / research prototype**, v0.1.0.

Runtime orchestration router that picks an execution strategy for an AI
coding task based on task features, repository characteristics, remaining
budget, and a latency target. It runs on top of
[microsoft/agent-framework][af] and does not replace it.

[af]: https://github.com/microsoft/agent-framework

## Motivation

Single-agent, multi-agent plan-and-verify, and parallel worker-pool
topologies have distinct cost, latency, and accuracy envelopes.
[agent-framework][af] ships all three as first-class primitives but
leaves topology selection to the developer at build time. This project
adds a runtime router so the topology is chosen per task, against a
hard USD budget and a latency target, with degradation rules when the
projected cost exceeds the remaining budget.

Evaluation targets include public coding benchmarks such as
[SWE-bench](https://www.swebench.com/) and the SWE-bench Verified
split. See [bench/README.md](bench/README.md) for the internal
benchmark harness used today; real-model results will be published
under `bench/results/` once the runner has executed against a live
provider (tracked in [docs/roadmap.md](docs/roadmap.md)).

## Strategies

| Strategy      | Shape                                      | When v0 picks it                |
|---------------|--------------------------------------------|---------------------------------|
| SingleAgent   | One streaming agent call                   | Default, large context, tight budget |
| PCIV          | Plan, critique, implement, verify pipeline | Reasoning-heavy tasks with tests |
| Fleet         | N parallel workers in git worktrees        | High file count, low coupling    |

All model ids are placeholder deployment names in
[config/policy.yaml](config/policy.yaml). Set your own via environment
variables or by editing the config; see
[docs/configuration.md](docs/configuration.md). Routing is a
hand-tuned decision tree in the same file for v0; a learned policy
is planned (see [docs/roadmap.md](docs/roadmap.md)).

## Quickstart

```bash
uv sync
export ANTHROPIC_API_KEY=sk-ant-...
uv run budgeteer run "Rename the User model to Account across src/" \
  --budget 2.50 --max-latency 600
```

Force a strategy for comparison:

```bash
uv run budgeteer run "..." --force-strategy single --dry-run
```

## How it composes with agent-framework

agent-budgeteer does not implement agents or chat clients. It classifies
a task, selects a strategy, and delegates execution through thin
adapters in `src/budgeteer/adapters/`. The `SingleAgent` and `Fleet`
strategies compose [agent-framework][af] primitives directly. The
`PCIV` strategy delegates to the sibling [pciv](https://github.com/patschmitt91/PCIV)
project, which today uses its own async pipeline; migration of that
spine to agent-framework graph workflow primitives is specified in
[PCIV/docs/decisions/0001-agent-framework-port.md](https://github.com/patschmitt91/PCIV/blob/master/docs/decisions/0001-agent-framework-port.md)
and tracked as a v0.2 milestone.

## What this is not

- Not a replacement for agent-framework
- Not a new agent runtime
- Not a prompt library

## Current status (v0)

Wired and exercised end-to-end (with mocked adapters in tests):

- Router, classifier, policy, and budget governor
- `SingleAgent` strategy against the Anthropic adapter
- `Fleet` strategy with SQLite ledger, sharding, and git-worktree workers
- `PCIV` strategy delegating to the `pciv` sibling project

Built but not yet wired into the default CLI path:

- `adapters/azure_openai_adapter.py` — satisfies the `StreamingChatClient`
  protocol but no router flag selects it yet.
- `learning.py` — trains a `DecisionTreeClassifier` policy from labeled
  examples; the router still uses the hand-tuned YAML decision tree.

Known gaps called out honestly:

- `bench/` validates routing accuracy only; it does not run real models
  or scan real repos. See `bench/README.md`.
- All tests stub at the adapter boundary. There are no HTTP-level
  integration tests yet.
- The classifier uses regex and wordlists only. A learned or
  LLM-extracted feature path is planned.

## Repository layout

```
src/budgeteer/          router, classifier, policy, budget, strategies, adapters
config/policy.yaml      thresholds, pricing, degradation rules
bench/                  10 benchmark tasks and runner
tests/                  unit tests for classifier, policy, budget, single_agent
docs/architecture.md    sequence diagram and scope notes
```

See `docs/architecture.md` for the routing pipeline and gaps called out
against agent-framework.

## License

MIT
