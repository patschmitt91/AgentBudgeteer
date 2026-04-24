# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog 1.1](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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

[Unreleased]: https://github.com/patschmitt91/AgentBudgeteer/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/patschmitt91/AgentBudgeteer/releases/tag/v0.1.0
