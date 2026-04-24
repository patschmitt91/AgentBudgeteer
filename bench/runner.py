"""Bench runner.

Loads each task YAML under ``bench/tasks/``, sends it through the router in
dry-run mode (classifier + policy, no model calls), and records whether
the selected strategy matches ``expected_strategy``. Projects cost for the
chosen strategy and a single-agent baseline for comparison.

Writes ``bench/results.json`` and prints a compact summary table.

Usage:
    uv run python bench/runner.py
    uv run python bench/runner.py --out bench/results.json --budget 5.0
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

BENCH_DIR = Path(__file__).resolve().parent
REPO_DIR = BENCH_DIR.parent

if str(REPO_DIR / "src") not in sys.path:  # pragma: no cover - import side effect
    sys.path.insert(0, str(REPO_DIR / "src"))

from budgeteer.budget import BudgetGovernor, load_degradation  # noqa: E402
from budgeteer.classifier import extract_features  # noqa: E402
from budgeteer.policy import Policy, RouteDecision  # noqa: E402
from budgeteer.pricing import PricingTable  # noqa: E402
from budgeteer.types import RepoSnapshot  # noqa: E402

POLICY_PATH = REPO_DIR / "config" / "policy.yaml"


@dataclass
class BenchTask:
    id: str
    description: str
    expected_strategy: str
    success_criteria: list[str] = field(default_factory=list)
    repo_fixture: str | None = None
    repo_stats: dict[str, Any] = field(default_factory=dict)
    budget_hint_usd: float | None = None
    latency_target_seconds: int = 600
    notes: str | None = None


@dataclass
class BenchResult:
    task_id: str
    expected_strategy: str
    actual_strategy: str
    actual_model: str
    routing_reason: str
    match: bool
    features: dict[str, Any]
    projected_cost_usd: float
    projected_cost_baseline_single: float
    projected_savings_vs_single: float
    degraded: bool
    notes: str | None = None


@dataclass
class BenchSummary:
    total: int
    matched: int
    mismatched: int
    accuracy: float
    by_strategy: dict[str, dict[str, int]]
    total_projected_cost_usd: float
    total_baseline_single_cost_usd: float
    total_savings_vs_single: float
    results: list[dict[str, Any]]


def load_task(path: Path) -> BenchTask:
    with path.open("r", encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}
    return BenchTask(
        id=str(raw["id"]),
        description=str(raw["description"]).strip(),
        expected_strategy=str(raw["expected_strategy"]).strip(),
        success_criteria=list(raw.get("success_criteria") or []),
        repo_fixture=raw.get("repo_fixture"),
        repo_stats=dict(raw.get("repo_stats") or {}),
        budget_hint_usd=raw.get("budget_hint_usd"),
        latency_target_seconds=int(raw.get("latency_target_seconds") or 600),
        notes=raw.get("notes"),
    )


def build_snapshot(task: BenchTask) -> RepoSnapshot:
    stats = dict(task.repo_stats)
    root = REPO_DIR
    if task.repo_fixture:
        candidate = (BENCH_DIR / task.repo_fixture).resolve()
        if candidate.exists():
            root = candidate
            scanned = _scan_fixture(candidate)
            for key, value in scanned.items():
                stats.setdefault(key, value)
    return RepoSnapshot(
        root=root,
        file_count=int(stats.get("file_count", 0) or 0),
        total_bytes=int(stats.get("total_bytes", 0) or 0),
        has_tests=bool(stats.get("has_tests", False)),
        has_type_config=bool(stats.get("has_type_config", False)),
        languages=list(stats.get("languages", []) or []),
    )


def _scan_fixture(root: Path) -> dict[str, Any]:
    files = [p for p in root.rglob("*") if p.is_file()]
    return {
        "file_count": len(files),
        "total_bytes": sum(p.stat().st_size for p in files),
        "has_tests": any("tests" in p.parts or p.name.startswith("test_") for p in files),
        "has_type_config": any(
            p.name in {"mypy.ini", "pyproject.toml", "tsconfig.json"} for p in files
        ),
    }


def _role_plan_for(strategy: str, decision: RouteDecision, policy: Policy) -> dict[str, str]:
    if strategy == "single":
        return {"single": decision.model}
    if strategy == "pciv":
        d = policy.defaults
        return {
            "plan": d.pciv_planner,
            "critique": d.pciv_implementer,
            "implement": d.pciv_implementer,
            "verify": d.pciv_planner,
        }
    if strategy == "fleet":
        return {f"worker_{i}": decision.model for i in range(4)}
    raise ValueError(f"unknown strategy {strategy!r}")


def run_task(task: BenchTask, policy: Policy, pricing: PricingTable, budget: float) -> BenchResult:
    snapshot = build_snapshot(task)
    features = extract_features(task.description, snapshot)
    applied_budget = task.budget_hint_usd if task.budget_hint_usd is not None else budget
    governor = BudgetGovernor(
        pricing=pricing,
        degradation=load_degradation(POLICY_PATH),
        hard_cap_usd=applied_budget,
    )
    decision = policy.route(features, governor.remaining, task.latency_target_seconds)

    chosen_plan = _role_plan_for(decision.strategy, decision, policy)
    chosen_proj = governor.project(decision.strategy, features, chosen_plan)

    baseline_decision = RouteDecision(
        strategy="single",
        model=policy.defaults.single_agent_primary,
        reason="baseline",
    )
    baseline_governor = BudgetGovernor(
        pricing=pricing,
        degradation=load_degradation(POLICY_PATH),
        hard_cap_usd=applied_budget,
    )
    baseline_plan = _role_plan_for("single", baseline_decision, policy)
    baseline_proj = baseline_governor.project("single", features, baseline_plan)

    return BenchResult(
        task_id=task.id,
        expected_strategy=task.expected_strategy,
        actual_strategy=decision.strategy,
        actual_model=decision.model,
        routing_reason=decision.reason,
        match=decision.strategy == task.expected_strategy,
        features=features.model_dump(),
        projected_cost_usd=round(chosen_proj.projected_cost_usd, 6),
        projected_cost_baseline_single=round(baseline_proj.projected_cost_usd, 6),
        projected_savings_vs_single=round(
            baseline_proj.projected_cost_usd - chosen_proj.projected_cost_usd, 6
        ),
        degraded=chosen_proj.degraded,
        notes=task.notes,
    )


def summarize(results: list[BenchResult]) -> BenchSummary:
    by_strategy: dict[str, dict[str, int]] = {}
    for r in results:
        bucket = by_strategy.setdefault(r.expected_strategy, {"matched": 0, "mismatched": 0})
        bucket["matched" if r.match else "mismatched"] += 1
    matched = sum(1 for r in results if r.match)
    total = len(results)
    return BenchSummary(
        total=total,
        matched=matched,
        mismatched=total - matched,
        accuracy=round(matched / total, 4) if total else 0.0,
        by_strategy=by_strategy,
        total_projected_cost_usd=round(sum(r.projected_cost_usd for r in results), 6),
        total_baseline_single_cost_usd=round(
            sum(r.projected_cost_baseline_single for r in results), 6
        ),
        total_savings_vs_single=round(sum(r.projected_savings_vs_single for r in results), 6),
        results=[asdict(r) for r in results],
    )


def run_all(tasks_dir: Path, budget: float) -> BenchSummary:
    policy = Policy.from_yaml(POLICY_PATH)
    pricing = PricingTable.from_yaml(POLICY_PATH)
    task_paths = sorted(tasks_dir.glob("task_*.yaml"))
    if not task_paths:
        raise FileNotFoundError(f"no bench tasks found in {tasks_dir}")
    results = [run_task(load_task(p), policy, pricing, budget) for p in task_paths]
    return summarize(results)


def _print_table(summary: BenchSummary) -> None:
    print(f"{'task':<40} {'expected':<8} {'actual':<8} {'match':<6} {'proj $':>10}")
    print("-" * 80)
    for row in summary.results:
        print(
            f"{row['task_id']:<40} "
            f"{row['expected_strategy']:<8} "
            f"{row['actual_strategy']:<8} "
            f"{'y' if row['match'] else 'n':<6} "
            f"{row['projected_cost_usd']:>10.4f}"
        )
    print("-" * 80)
    print(
        f"accuracy {summary.accuracy * 100:.1f}% "
        f"({summary.matched}/{summary.total})  "
        f"total projected ${summary.total_projected_cost_usd:.4f}  "
        f"single-baseline ${summary.total_baseline_single_cost_usd:.4f}  "
        f"savings ${summary.total_savings_vs_single:.4f}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the agent-budgeteer benchmark suite.")
    parser.add_argument(
        "--tasks-dir",
        type=Path,
        default=BENCH_DIR / "tasks",
        help="Directory with task_*.yaml files.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=BENCH_DIR / "results.json",
        help="Where to write results.json.",
    )
    parser.add_argument(
        "--budget",
        type=float,
        default=5.0,
        help="Default hard-cap USD budget when a task does not set budget_hint_usd.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the per-task table.",
    )
    args = parser.parse_args(argv)

    summary = run_all(args.tasks_dir, args.budget)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        json.dump(asdict(summary), f, indent=2, sort_keys=False)

    if not args.quiet:
        _print_table(summary)
        print(f"wrote {args.out}")
    return 0 if summary.mismatched == 0 else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
