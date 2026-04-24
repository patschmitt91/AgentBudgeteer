"""Tests for the bench runner."""

from __future__ import annotations

import json
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parents[1] / "bench"
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))

from runner import (  # noqa: E402
    BenchTask,
    build_snapshot,
    load_task,
    main,
    run_all,
)

REPO_DIR = BENCH_DIR.parent
TASKS_DIR = BENCH_DIR / "tasks"


def test_every_bench_task_yaml_parses() -> None:
    paths = sorted(TASKS_DIR.glob("task_*.yaml"))
    assert len(paths) == 10
    for p in paths:
        task = load_task(p)
        assert task.id
        assert task.description
        assert task.expected_strategy in {"single", "pciv", "fleet"}
        assert task.success_criteria


def test_snapshot_honors_repo_stats() -> None:
    task = BenchTask(
        id="x",
        description="n/a",
        expected_strategy="single",
        repo_stats={
            "file_count": 7,
            "total_bytes": 1234,
            "has_tests": True,
        },
    )
    snap = build_snapshot(task)
    assert snap.file_count == 7
    assert snap.total_bytes == 1234
    assert snap.has_tests is True


def test_run_all_matches_expected_strategies_for_all_bench_tasks() -> None:
    summary = run_all(TASKS_DIR, budget=5.0)
    assert summary.total == 10
    # The bench fixtures are tuned to exercise each policy branch; routing
    # must match every expected_strategy without running real model calls.
    assert summary.mismatched == 0, _format_mismatches(summary)
    assert summary.accuracy == 1.0
    # Projection should be non-zero for each task.
    for row in summary.results:
        assert row["projected_cost_usd"] > 0
        assert row["projected_cost_baseline_single"] > 0


def test_main_writes_results_json(tmp_path: Path) -> None:
    out = tmp_path / "results.json"
    code = main(["--tasks-dir", str(TASKS_DIR), "--out", str(out), "--quiet"])
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["total"] == 10
    assert payload["matched"] == 10


def _format_mismatches(summary: object) -> str:
    rows = getattr(summary, "results", [])
    bad = [r for r in rows if not r.get("match")]
    return "mismatches:\n" + "\n".join(
        f"  {r['task_id']}: expected={r['expected_strategy']} actual={r['actual_strategy']} features={r['features']}"
        for r in bad
    )
