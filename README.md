# agent-budgeteer

Runtime orchestration router that picks an execution strategy for an AI
coding task based on task features, repository characteristics, remaining
budget, and a latency target. It runs on top of
[microsoft/agent-framework](https://github.com/microsoft/agent-framework)
and does not replace it.

## Why this exists

- Princeton NLP evaluated single-agent versus multi-agent systems on coding
  benchmarks and found that single agents match or beat multi-agent systems
  on roughly 64% of tasks when given equivalent tools and context. The
  multi-agent lift is around 2.1 percentage points of accuracy at about 2x
  the cost. Picking the right pattern matters.
- GitHub's Rubber Duck experiment paired Claude Sonnet with GPT-5.4 and
  closed 74.7% of the Sonnet-to-Opus gap on SWE-Bench Pro. Cross-family
  orchestration helps on the hard tail but is overkill for simple edits.
- agent-framework ships the orchestration patterns. No existing framework
  decides at runtime which pattern to use for a given task. That is what
  agent-budgeteer adds.

## Strategies

| Strategy      | Shape                                      | When v0 picks it                |
|---------------|--------------------------------------------|---------------------------------|
| SingleAgent   | One Opus 4.7 call with streaming           | Default, large context, tight budget |
| PCIV          | Plan, critique, implement, verify graph    | Reasoning-heavy tasks with tests |
| Fleet         | N parallel workers in git worktrees        | High file count, low coupling    |

Routing is a hand-tuned decision tree in `config/policy.yaml` for v0. A
learned policy is planned.

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

agent-budgeteer does not implement agents or chat clients. It classifies a
task, selects a strategy, and then delegates execution to agent-framework
primitives through thin adapters in `src/budgeteer/adapters/`. The PCIV
strategy wraps an agent-framework graph workflow. The Fleet strategy spawns
agent-framework agents across git worktrees.

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
