# Contributing

## Dev setup

```
uv sync --extra dev
```

`agent-budgeteer` depends on [`pciv`](https://github.com/patschmitt91/PCIV)
pinned to a git SHA in `pyproject.toml`. For local editing against a
sibling checkout, uncomment the `tool.uv.sources` override:

```toml
[tool.uv.sources]
pciv = { path = "../PCIV", editable = true }
```

then re-run `uv sync --extra dev`. Keep that line commented in commits;
fresh clones without a sibling `PCIV` directory must resolve `pciv`
from the git pin.

## Checks before opening a PR

```
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy
uv run pytest -q
```

## Filing issues

Open an issue at
<https://github.com/patschmitt91/AgentBudgeteer/issues>. Use the
Bug Report or Feature Request template.
