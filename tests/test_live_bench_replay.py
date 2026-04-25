"""Replay-only test for the live-provider micro-bench (AB-6).

Discovers every task under ``bench/live/tasks/`` and replays it through
``bench.live.runner.run_replay``. Tasks without a recorded cassette are
skipped per-task with a clear reason (not a hard failure) so the test
infra ships before the first cassette is recorded.

Recording is gated and out of scope for CI — see
``bench/live/README.md``. This test never sets ``BENCH_LIVE`` and never
constructs an ``AnthropicAdapter``, so it has no API-key dependency.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_DIR = Path(__file__).resolve().parents[1]
if str(REPO_DIR) not in sys.path:  # pragma: no cover
    sys.path.insert(0, str(REPO_DIR))

from bench.live.runner import (  # noqa: E402
    CASSETTES_DIR,
    TASKS_DIR,
    LiveBenchTask,
    run_replay,
)


def _task_files() -> list[Path]:
    if not TASKS_DIR.is_dir():
        return []
    return sorted(TASKS_DIR.glob("*.yaml"))


@pytest.mark.parametrize(
    "task_path",
    _task_files(),
    ids=lambda p: p.stem,
)
def test_live_bench_task_replays_cleanly(task_path: Path) -> None:
    task = LiveBenchTask.load(task_path)
    cassette_path = CASSETTES_DIR / f"{task.id}.json"
    if not cassette_path.is_file():
        pytest.skip(
            f"no cassette at {cassette_path.relative_to(REPO_DIR)} yet; "
            f"see bench/live/README.md for the recording protocol "
            f"(provider={task.provider}, model={task.model}, "
            f"cap=${task.cost_cap_usd:.2f})."
        )
    report = run_replay(task, cassette_path=cassette_path)
    assert report.success, (
        f"replay failed: strategy_match={report.strategy_match} "
        f"cost_under_cap={report.cost_under_cap} "
        f"actual_cost_usd={report.actual_cost_usd:.6f} "
        f"cap_usd={report.cap_usd:.6f} "
        f"notes={report.notes!r}"
    )
    assert report.actual_strategy == task.expected_strategy
    assert report.actual_cost_usd <= task.cost_cap_usd
