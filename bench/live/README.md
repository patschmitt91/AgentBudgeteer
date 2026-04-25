# bench/live — first live-provider micro-bench (AB-6)

This directory holds the **live-provider** bench surface. It exists to
turn the project's "no live results" gap into "we have one cassette,
here are the numbers, here's how to add more."

## Status

Scaffolding only. **No cassette has been recorded yet.** CI runs the
replay test, which auto-skips per-task when its cassette is missing.
Recording requires a real API key, an explicit hard cap, and a
person-in-the-loop confirmation. See [Recording](#recording-a-cassette).

## Why hand-rolled cassettes (no vcrpy)

The session prompt called this out explicitly: vcrpy needs
justification. Hand-rolled won out:

| Concern                       | vcrpy                          | Hand-rolled                       |
|-------------------------------|--------------------------------|-----------------------------------|
| New dependency                | vcrpy + httpx bridge           | none                              |
| Stable across SDK upgrades    | breaks on HTTP body changes    | captures `(messages, model) → AdapterResponse` |
| `git diff` friendliness       | YAML blobs of HTTP bodies      | structured JSON                   |
| Coupling to provider internals| HTTP-level                     | adapter Protocol level            |
| Existing fake-adapter pattern | unused                         | reused (`tests/test_cli_e2e.py`)  |

We already had a clean injection seam at `AnthropicAdapter`. The
cassette format is documented in [`cassette.py`](cassette.py).

## Layout

```
bench/live/
├── README.md                 # this file
├── __init__.py               # makes the package importable
├── cassette.py               # Cassette / CassetteAdapter / RecordingAdapter
├── runner.py                 # CLI entrypoint + run_replay + run_live
├── tasks/                    # one YAML per bench task
│   └── task_01_reverse_string.yaml
├── cassettes/                # one JSON per recorded task (gitignored except .gitkeep)
└── .ledger/                  # per-task PersistentBudgetLedger files (gitignored)
```

## Replay (default; CI)

```pwsh
uv run python -m bench.live.runner task_01_reverse_string
```

Loads `tasks/task_01_reverse_string.yaml`, opens
`cassettes/task_01_reverse_string.json` (or exits 2 if missing),
constructs a `CassetteAdapter`, and runs the task through the existing
`Router` with the strategy forced to the task's `expected_strategy`.

Asserts:

1. `outcome.result.success` is `True`.
2. `actual_cost_usd <= cost_cap_usd`.
3. `actual_cost_usd == cassette.totals.cost_usd` (within 1e-9).
4. `policy.route(...)` would have selected `expected_strategy` from
   the classifier features alone (no force).

The CI test [`tests/test_live_bench_replay.py`](../../tests/test_live_bench_replay.py)
discovers every task under `tasks/` and runs the replay path; tasks
without a cassette are individually skipped.

## Recording a cassette

**This is gated.** Do not run blind — costs real money and burns API
quota. Process:

1. **Confirm the task fixture.** Edit the task YAML if the prompt
   needs to change. Lower `cost_cap_usd` if the model's pricing in
   `config/policy.yaml` has shifted.
2. **Set the API key.** `$env:ANTHROPIC_API_KEY = "..."`.
3. **Record:**

   ```pwsh
   $env:BENCH_LIVE = "1"
   uv run python -m bench.live.runner task_01_reverse_string
   Remove-Item Env:BENCH_LIVE
   ```

The runner:

- Opens a per-task `PersistentBudgetLedger` at
  `.ledger/<task_id>.db` with `window=daily, cap_usd=task.cost_cap_usd`.
- Wraps the real `AnthropicAdapter` with `RecordingAdapter`. Every
  call charges the ledger AFTER the response (real cost is only known
  post-response) and BEFORE persisting to the cassette.
- On `BudgetExceeded` the cassette is **not written**; the runner
  prints `"hard cap breached … cassette NOT written"` and exits 1.
- On clean completion the cassette is written to
  `cassettes/<task_id>.json`.

`.ledger/` accumulates across runs in the daily window so an operator
re-running a recording several times in one day still shares the cap.
Delete `.ledger/<task_id>.db` to reset.

## Adding a new task

1. Drop a YAML under `tasks/` following the schema in
   [task_01_reverse_string.yaml](tasks/task_01_reverse_string.yaml).
2. Pick a model id that exists in `config/policy.yaml`'s `pricing`
   block (the runner's cost lookup is verbatim).
3. Set a conservative `cost_cap_usd`. For Anthropic Sonnet-class:
   $0.05 covers ~3K output tokens. For cheaper tiers: scale down.
4. Record per the section above.
5. The replay test auto-discovers the new task — no test edits
   needed.

## What this bench does NOT do

- **Does not measure quality.** A green cassette only proves cost +
  routing held; it says nothing about whether the model's answer was
  good. Adding a quality grader is out of scope for the first cut.
- **Does not exercise PCIV / Fleet strategies.** Those make many
  adapter calls per run; the cassette format supports it (calls are
  ordered, mismatch raises) but the first task is single-call so
  failure modes are easy to reason about.
- **Does not bypass `policy.yaml` pricing.** The cost asserted in the
  cassette is computed by `PricingTable.cost(...)` from the recorded
  token counts. If the policy's pricing changes, the recorded
  `totals.cost_usd` becomes stale and replay will warn. Re-record or
  pin pricing in the task YAML in a future iteration.
