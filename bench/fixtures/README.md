# bench fixtures

Every task YAML in `bench/tasks/` references a `repo_fixture:` path that
points into this directory (e.g. `fixtures/small_app/`, `fixtures/legacy_api/`).

None of those fixtures exist yet. The bench runner currently operates in
dry-run mode and synthesizes a `Features` record from the task YAML
without reading any files from here.

To make the bench end-to-end real:

1. Add 2–3 small self-contained sample repos under this directory.
2. Point each task YAML's `repo_fixture:` at one of them.
3. Extend `bench/runner.py` to build the `Features` record by actually
   scanning the fixture repo instead of trusting the YAML.
4. Add a `--live` flag that then executes the selected strategy against
   a sandboxed adapter and records cost, latency, and a pass/fail signal.

Until then, `results.json` reflects routing-policy regression only, not
end-to-end performance.
