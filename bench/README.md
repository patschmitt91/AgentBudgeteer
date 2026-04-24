# bench

Ten fixture tasks that exercise different regions of the routing policy.
Each task YAML lists the expected strategy so the decision tree can be
regression-tested without running real model calls.

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

Planned follow-ups:

- Populate `bench/fixtures/` with 2–3 small real repos so the classifier
  can read actual file contents.
- Wire a `--live` mode in `runner.py` that executes the selected strategy
  against a sandboxed adapter and measures cost, latency, and a simple
  pass/fail signal.
