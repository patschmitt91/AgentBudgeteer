# bench

Ten fixture tasks that exercise different regions of the routing policy.
Each task YAML lists the expected strategy so the decision tree can be
regression-tested without running real model calls.

A separate **live-provider micro-bench** lives under
[`bench/live/`](live/README.md) — see that README for the cassette
recording / replay protocol (AB-6).

## Scope and honesty

This bench currently validates **routing accuracy only**, not end-to-end
task completion. `runner.py` runs in dry-run mode: it loads each task,
builds a synthetic `Features` record from the YAML, invokes the policy,
and compares the selected strategy to `expected_strategy`. No Anthropic
or Azure OpenAI calls are made, and no repo fixtures are scanned.

The `repo_fixture:` fields in the task YAMLs point at `bench/fixtures/…`
paths that are not populated in this repo yet. Until those land, treat
`results.json` as a routing-policy regression signal, not an end-to-end
benchmark.

End-to-end coverage now lives in [`bench/live/`](live/README.md), which
ships:

- A hand-rolled JSON cassette format keyed at the
  `AnthropicAdapter` Protocol level (no vcrpy, no SDK coupling).
- `tests/test_live_bench_replay.py` — a CI-active replay test that
  auto-discovers tasks under `bench/live/tasks/` and skips per-task
  when no cassette has been recorded yet.
- `bench/live/runner.py` — gated live-recording entry point that
  enforces a per-task hard cap via
  `agentcore.budget.PersistentBudgetLedger` BEFORE the cassette is
  persisted.

Planned follow-ups:

- Populate `bench/fixtures/` with 2–3 small real repos so the classifier
  can read actual file contents.
- Wire a `--live` mode in `runner.py` that executes the selected strategy
  against a sandboxed adapter and measures cost, latency, and a simple
  pass/fail signal.
