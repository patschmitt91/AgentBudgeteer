# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog 1.1](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] — 2026-04-24

### Added

- Structured logging: `JsonFormatter` in `src/budgeteer/telemetry.py`
  emits `ts`, `level`, `logger`, `msg`, plus `run_id`, `trace_id`, and
  `span_id` when an OTel span is active. Root CLI callback accepts
  `--verbose`/`--quiet` for DEBUG/WARNING, honors `LOG_FORMAT=json|text`
  (default `text` on a TTY, `json` otherwise), and attaches a
  `RedactionFilter` to every handler.
- Central redaction helper `src/budgeteer/redaction.py` scrubbing `sk-`
  API keys, bearer tokens, JWTs, 40+ char hex blobs, and literal values
  of secret-named env vars (`AZURE_OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
  `OPENAI_API_KEY`, `APPLICATIONINSIGHTS_CONNECTION_STRING`, and any
  name containing `KEY`/`SECRET`/`TOKEN`/`PASSWORD`/`CONNECTION_STRING`).
  Applied to log records and to span attribute dicts via `redact_mapping`.
- `tests/test_secret_redaction.py` seeds three distinct secret shapes
  (sk- key, bearer token, JWT) into env + the task prompt, runs the
  full dry-run pipeline, and asserts zero occurrences in captured logs
  or span attributes / events.
- `budgeteer doctor` subcommand: reports Python version, `uv` version,
  git availability, OS, policy resolution, and redacted env-var
  presence as JSON. Exits 0 only when python/uv/git/config checks pass.
- OTel counters in `budgeteer.telemetry`: `runs_total`,
  `runs_failed_total`, `budget_usd_spent_total`,
  `routing_decisions_total{strategy}`. `tests/test_metrics.py` uses an
  `InMemoryMetricReader` to assert each name appears after CLI
  invocations.
- Multi-stage `Dockerfile` at the repo root: builder uses `uv sync
  --no-dev --frozen`; runtime is `python:3.12-slim`, non-root uid 1001,
  healthcheck runs `budgeteer doctor`. `.dockerignore` excludes
  `.venv`, `.git`, `dist`, `tests`, `docs`, and caches.
- CI `docker` job builds the image on `ubuntu-latest` and runs
  `docker run --rm <image> doctor`; nothing is pushed.

- `SECURITY.md` at the repo root: supported-versions table, private
  reporting channels (GitHub private advisories + maintainer email),
  and a 90-day coordinated-disclosure window. Linked from the README.
- Top-level `justfile` with `install`, `lint`, `fmt`, `typecheck`,
  `test`, `cov`, `build`, and `clean` recipes; all shell out to `uv`.
  README has a new `Development` section documenting the recipes.
- `src/budgeteer/py.typed` marker, force-included in the wheel via
  `[tool.hatch.build.targets.wheel.force-include]` so downstream type
  checkers pick up the package as typed.
- `[project.urls]` now includes `Homepage`, `Source`, `Issues`, and
  `Changelog` (previously only `Homepage`, `Repository`, `Issues`).
- `twine==5.1.1` pinned in the `dev` extra.
- CI `build` job on ubuntu-latest runs `uv build` followed by
  `uv run twine check dist/*` and uploads `dist/` as an artifact.
- `release.yml` split into `build` (runs `uv build` + `twine check` +
  uploads `dist/` artifact) and `release` (downloads the artifact and
  creates the GitHub Release via `softprops/action-gh-release@v2`).
  The release job `needs: build`, so twine metadata failures block
  the GitHub Release.
- `.github/workflows/release.yml` — on tag `v*`, build the wheel with
  `uv build` and upload it as a GitHub Release asset (no PyPI publish).
- CI `type-check` job runs `uv run mypy` independently on
  ubuntu-latest and windows-latest, so type errors no longer hide
  inside the test matrix.
- CI `pre-commit` job runs `pre-commit run --all-files` on every push
  and PR.
- CI `docs-check` job runs `lycheeverse/lychee-action@v2` against
  `README.md`, `docs/**/*.md`, and `bench/**/*.md`.
- CI `bench-smoke` job runs `uv run python -m bench.smoke` (dry-run,
  one-minute timeout) and fails if routing accuracy or any per-strategy
  matched count regresses against `bench/results.baseline.json`.
- `bench/__init__.py` and `bench/smoke.py` to support the smoke check.
- `bench/results.baseline.json` — current baseline (10/10 matched,
  accuracy 1.0).
- `pytest-cov` and `pre-commit` pinned in the `dev` extra; coverage
  configured through `[tool.pytest.ini_options].addopts` with
  `--cov=src/budgeteer --cov-report=term-missing --cov-fail-under=85`.
- `tests/test_cli_e2e.py` exercising the full `budgeteer` Typer CLI
  (dry-run, forced strategy, executed single-agent run with a fake
  adapter, `learn` command, repo scanner, policy path resolver).
- `tests/test_adapters_and_worktree.py` covering the Anthropic adapter
  streaming path, the `GitWorktreeManager` git and fallback branches,
  and `TempDirWorktreeManager` lifecycle.
- `tests/test_readme_examples.py` parsing README code blocks and
  asserting every recognized shell command parses via `shlex` and its
  executable resolves on PATH (or is a project CLI).
- `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1).
- `.pre-commit-config.yaml` wiring ruff, ruff-format, mypy (via
  `uv run mypy`), and the standard `pre-commit-hooks` whitespace /
  merge-conflict / toml / yaml checks.
- `docs/configuration.md` — every key in `config/policy.yaml`
  documented, including env-var overrides.
- `docs/roadmap.md` — dated v0.1 / v0.2 / v0.3 milestones.
- `docs/decisions/0001-decision-tree-not-classifier.md`,
  `0002-sqlite-ledger-for-fleet.md`,
  `0003-git-worktrees-not-branches-for-fleet.md`.

### Changed

- Rewrote `README.md` to the 13-section skeleton (pitch, badges,
  status, what it does, why, install, quickstart with expected
  output, architecture Mermaid sequence, strategies table,
  configuration pointer, benchmarks, roadmap, license + BibTeX).
- Bumped the `pciv` git pin from `2a64bfe` to `5c04e8e` so fresh
  `uv sync` pulls in PCIV's Phase-1b and Phase-2 hygiene commits
  (CoC, pre-commit, LICENSE, docs, ADRs).
- Bumped classifier from `Development Status :: 3 - Alpha` to
  `Development Status :: 4 - Beta` and added
  `Operating System :: OS Independent`.

## [0.1.0] — 2026-04-24

### Added

- Router, classifier, policy loader, and budget governor.
- `SingleAgent` strategy backed by the Anthropic adapter.
- `Fleet` strategy with SQLite shard ledger and git-worktree workers.
- `PCIV` strategy delegating to the external
  [pciv](https://github.com/patschmitt91/PCIV) project through a
  pure-function runner boundary.
- OpenTelemetry span emission with optional Azure Monitor export.
- `budgeteer` CLI (`run`, `policy show`, etc.).
- Benchmark harness under `bench/` with 10 routing-accuracy tasks.

### Changed

- `pciv` dependency is now pinned to a git commit instead of a
  relative editable path, so `uv sync` works on a fresh clone.
- Default model identifiers in `config/policy.yaml` are now
  role-based placeholders (`anthropic-primary`, `azure-reasoning`,
  etc.) and must be overridden with real deployment names.

### Removed

- README bullets referencing specific empirical claims that could
  not be cited to a public source.

[Unreleased]: https://github.com/patschmitt91/AgentBudgeteer/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/patschmitt91/AgentBudgeteer/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/patschmitt91/AgentBudgeteer/releases/tag/v0.1.0
