"""Typer CLI entrypoint. `budgeteer run "<task>" --budget 2.50 ...`"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import typer

from budgeteer.router import Router
from budgeteer.types import RepoSnapshot

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="agent-budgeteer: runtime router over microsoft/agent-framework.",
)


_TASK_ARG = typer.Argument(..., help="Task description in natural language.")
_BUDGET_OPT = typer.Option(2.50, "--budget", help="Hard USD cap for the run.")
_LATENCY_OPT = typer.Option(600, "--max-latency", help="Latency target in seconds.")
_FORCE_OPT = typer.Option(
    None, "--force-strategy", help="Override routing. One of: single, pciv, fleet."
)
_DRY_RUN_OPT = typer.Option(False, "--dry-run", help="Classify and route without executing.")
_REPO_OPT = typer.Option(None, "--repo", help="Path to the repository snapshot root.")
_POLICY_OPT = typer.Option(None, "--policy", help="Path to policy.yaml (default: auto-detect).")
_PCIV_CONFIG_OPT = typer.Option(
    None,
    "--pciv-config",
    help="Path to pciv plan.yaml (overrides the value in policy.yaml).",
)


def _default_policy_path() -> Path:
    here = Path(__file__).resolve()
    # Walk upward looking for config/policy.yaml.
    for parent in [here.parent, *here.parents]:
        candidate = parent / "config" / "policy.yaml"
        if candidate.is_file():
            return candidate
    # Final fallback: assume CWD.
    return Path.cwd() / "config" / "policy.yaml"


_IGNORED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".tox",
        ".nox",
        "dist",
        "build",
        ".next",
        ".cache",
        "target",
    }
)


def _scan_repo(root: Path) -> RepoSnapshot:
    if not root.exists():
        return RepoSnapshot(root=root)
    file_count = 0
    total_bytes = 0
    has_tests = False
    has_type_config = False
    languages: set[str] = set()
    # os.walk lets us prune ignored directories in place; rglob does not.
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORED_DIRS]
        dpath = Path(dirpath)
        for name in filenames:
            path = dpath / name
            if name in {"mypy.ini", "tsconfig.json", "pyproject.toml"}:
                has_type_config = True
            if "test" in path.parts or name.startswith("test_"):
                has_tests = True
            suffix = path.suffix.lower().lstrip(".")
            if suffix in {"py", "ts", "tsx", "js", "jsx", "go", "rs", "java", "cs", "rb"}:
                languages.add(suffix)
            file_count += 1
            try:
                total_bytes += path.stat().st_size
            except OSError:
                continue
    return RepoSnapshot(
        root=root,
        file_count=file_count,
        total_bytes=total_bytes,
        has_tests=has_tests,
        has_type_config=has_type_config,
        languages=sorted(languages),
    )


@app.command()
def version() -> None:
    """Print the package version."""
    from budgeteer import __version__

    typer.echo(__version__)


_TRAIN_DATA_ARG = typer.Argument(
    ...,
    help="Path to a bench results.json, a labeled list.json, or JSONL training data.",
)
_TRAIN_OUT_OPT = typer.Option(
    None,
    "--out",
    help="Optional path to write a JSON training report.",
)
_TRAIN_MAX_DEPTH_OPT = typer.Option(5, "--max-depth", help="Max decision tree depth.")
_TRAIN_MIN_LEAF_OPT = typer.Option(1, "--min-samples-leaf", help="Minimum samples per leaf.")


@app.command()
def learn(
    data: Path = _TRAIN_DATA_ARG,
    policy: Path | None = _POLICY_OPT,
    out: Path | None = _TRAIN_OUT_OPT,
    max_depth: int = _TRAIN_MAX_DEPTH_OPT,
    min_samples_leaf: int = _TRAIN_MIN_LEAF_OPT,
) -> None:
    """Train a DecisionTreeClassifier from labeled examples and print the report."""

    if not data.is_file():
        typer.echo(f"training data not found: {data}", err=True)
        raise typer.Exit(code=2)

    policy_path = policy or _default_policy_path()
    if not policy_path.is_file():
        typer.echo(f"policy file not found: {policy_path}", err=True)
        raise typer.Exit(code=2)

    from budgeteer.learning import load_examples, train_policy
    from budgeteer.policy import Policy

    defaults = Policy.from_yaml(policy_path).defaults
    examples = load_examples(data)
    learned = train_policy(
        examples,
        defaults,
        max_depth=max_depth,
        min_samples_leaf=min_samples_leaf,
    )
    payload = {
        "samples": learned.report.samples,
        "label_counts": learned.report.label_counts,
        "train_accuracy": learned.report.train_accuracy,
        "feature_importances": learned.report.feature_importances,
        "tree_depth": learned.report.tree_depth,
        "leaf_count": learned.report.leaf_count,
        "class_labels": learned.report.class_labels,
    }
    text = json.dumps(payload, indent=2, sort_keys=False)
    typer.echo(text)
    if out is not None:
        if out.exists() and out.is_dir():
            typer.echo(f"--out points to a directory, not a file: {out}", err=True)
            raise typer.Exit(code=2)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")


@app.command()
def run(
    task: str = _TASK_ARG,
    budget: float = _BUDGET_OPT,
    max_latency: int = _LATENCY_OPT,
    force_strategy: str | None = _FORCE_OPT,
    dry_run: bool = _DRY_RUN_OPT,
    repo: Path | None = _REPO_OPT,
    policy: Path | None = _POLICY_OPT,
    pciv_config: Path | None = _PCIV_CONFIG_OPT,
) -> None:
    """Run a task through the router."""

    if force_strategy is not None and force_strategy not in {"single", "pciv", "fleet"}:
        typer.echo(f"invalid --force-strategy {force_strategy!r}", err=True)
        raise typer.Exit(code=2)

    policy_path = policy or _default_policy_path()
    if not policy_path.is_file():
        typer.echo(f"policy file not found: {policy_path}", err=True)
        raise typer.Exit(code=2)

    repo_root = repo if repo is not None else Path.cwd()
    snapshot = _scan_repo(repo_root)
    router = Router(
        policy_path=policy_path,
        budget_cap_usd=budget,
        pciv_config_path=pciv_config,
    )

    if dry_run:
        features, decision = router.route_only(
            task=task,
            repo_snapshot=snapshot,
            latency_target_seconds=max_latency,
            forced=force_strategy,
        )
        payload = {
            "dry_run": True,
            "features": features.model_dump(),
            "decision": {
                "strategy": decision.strategy,
                "model": decision.model,
                "reason": decision.reason,
            },
        }
        typer.echo(json.dumps(payload, indent=2, default=str))
        return

    outcome = router.run(
        task=task,
        repo_snapshot=snapshot,
        latency_target_seconds=max_latency,
        forced=force_strategy,
    )
    payload = {
        "dry_run": False,
        "features": outcome.features.model_dump(),
        "decision": {
            "strategy": outcome.decision.strategy,
            "model": outcome.decision.model,
            "reason": outcome.decision.reason,
        },
        "result": outcome.result.model_dump(mode="json"),
    }
    typer.echo(json.dumps(payload, indent=2, default=str))
    if not outcome.result.success:
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    app()
