# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog 1.1](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1).
- `.pre-commit-config.yaml` wiring ruff, ruff-format, mypy (via
  `uv run mypy`), and the standard `pre-commit-hooks` whitespace /
  merge-conflict / toml / yaml checks.

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
