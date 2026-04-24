# agent-budgeteer task runner.
#
# Install `just` from https://github.com/casey/just and run `just <target>`.
# All targets delegate to `uv` so no global Python install is required.

default:
    @just --list

# Sync the dev + learn extras into .venv via uv.
install:
    uv sync --extra dev --extra learn

# Lint with ruff.
lint:
    uv run ruff check src tests

# Auto-format with ruff.
fmt:
    uv run ruff format src tests

# Static type check with mypy.
typecheck:
    uv run mypy

# Run the full pytest suite (coverage gate configured in pyproject.toml).
test:
    uv run pytest

# Run pytest with a terminal coverage report and XML output.
cov:
    uv run pytest --cov=src/budgeteer --cov-report=term-missing --cov-report=xml

# Build sdist + wheel into dist/.
build:
    uv build

# Remove build, cache, and coverage artifacts.
clean:
    python -c "import shutil, pathlib; [shutil.rmtree(p, ignore_errors=True) for p in ['dist', 'build', '.pytest_cache', '.mypy_cache', '.ruff_cache', 'htmlcov']]; pathlib.Path('.coverage').unlink(missing_ok=True); pathlib.Path('coverage.xml').unlink(missing_ok=True)"
