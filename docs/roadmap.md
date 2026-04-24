# Roadmap

Dated, best-effort milestones. Dates slip; scope is load-bearing.

## v0.1.0 — 2026-04-24 (shipped)

- Router, classifier, policy loader, budget governor.
- `SingleAgent` strategy against Anthropic.
- `Fleet` strategy with SQLite shard ledger and git worktrees.
- `PCIV` strategy delegating to the sibling `pciv` project.
- Routing-accuracy bench harness (10 fixture tasks, dry-run only).
- Repo hygiene: CI matrix (Ubuntu + Windows, Py3.11 / 3.12), CodeQL,
  Dependabot, pre-commit, Code of Conduct.

## v0.2.0 — target Q3 2026

**Theme: replace synthetic signal with live signal.**

- Populate `bench/fixtures/` with 2–3 small real repos so the classifier
  reads actual file contents, not synthetic features.
- Add `--live` mode to `bench/runner.py` that executes each selected
  strategy against a sandboxed adapter; measure cost, latency, pass/fail.
- Publish live-provider results as JSON under `bench/results/`.
- Train a `LearnedPolicy` on the published results and wire a
  `--policy learned` flag on the CLI so router selection can be compared
  against the hand-tuned decision tree.
- Wire the already-built `adapters/azure_openai_adapter.py` into at
  least one strategy flag so the router can target Azure OpenAI end-to-end.

## v0.3.0 — target Q1 2027

**Theme: production readiness.**

- Budget governor enforces across concurrent runs (not just per run),
  backed by a shared SQLite or Redis token bucket.
- Per-strategy circuit breakers on repeated adapter failures.
- First-class cancellation token plumbed through strategies and adapters.
- Cost and latency SLO dashboards as an Application Insights workbook.

## Out of scope

- Replacing `microsoft/agent-framework` with a different orchestration
  runtime. See the composition gaps in
  [docs/architecture.md](architecture.md).
- Prompt library / prompt registry. Prompts remain local to the strategy
  that uses them.
- A UI. The CLI is the interface.
