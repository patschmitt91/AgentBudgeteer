"""Integration test for the cross-run rolling-window budget cap (AB-2 / ADR 0005).

Two sequential `budgeteer run` invocations share a SQLite-backed
``PersistentBudgetLedger`` configured via ``[cross_run]`` in
``policy.yaml``. The first run completes within the cap; the second is
fail-fast at preflight because the recorded spend from run 1 left no
headroom.

A third invocation with ``--ignore-cross-run-cap`` proves the emergency
override succeeds and writes a ``forced=1`` audit row.

The fake adapter from ``test_cli_e2e.py`` returns 100 input + 50 output
tokens per call; with the price overrides below ($1.0/MTok) each run
costs $1.5e-4, so a $2e-4 cap admits exactly one run.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from budgeteer.cli import app
from test_cli_e2e import (  # noqa: I001
    POLICY_PATH,
    _FakeAdapter,  # noqa: F401  (used indirectly via _patch_anthropic_adapter)
)

# 100 + 50 = 150 tokens / 1M * $1.0/MTok = $1.5e-4 per call. Single-agent
# strategy makes one adapter call per run, so per-run *actual* spend is
# $1.5e-4 — but the strategy's pre-execute projection (single_base=1500
# output tokens at $1/MTok, plus input) is ~$0.0015, so the cross-run cap
# must comfortably exceed the projection or the single-agent governor's
# `check_can_start` aborts before any real spend happens.
_PER_RUN_SPEND_USD = 1.5e-4
# Cross-run cap: large enough to fit run 1's $0.0015 projection. We then
# top the ledger up to the cap before run 2 to deterministically exhaust
# the window without depending on projection / actual sequencing.
_MONTHLY_CAP_USD = 0.005
# Per-run --budget: comfortably above the per-run actual spend so the
# per-run governor never trips. The cross-run-aware effective budget
# (computed at CLI entry) further caps this to the window's remaining.
_PER_RUN_BUDGET_USD = 0.10


def _write_test_policy(
    src_policy: Path,
    dst_policy: Path,
    *,
    cap_usd: float | None,
    cross_run_db: Path,
) -> None:
    """Copy the project policy.yaml and overwrite the cross-run block,
    pricing block, and pciv pointer for hermetic test isolation."""

    raw = yaml.safe_load(src_policy.read_text(encoding="utf-8"))
    if cap_usd is None:
        raw.pop("cross_run", None)
    else:
        raw["cross_run"] = {
            "cap_usd": cap_usd,
            "window": "monthly",
            "db_path": str(cross_run_db),
        }
    # Cheap pricing so the test math is human-readable.
    for model in list(raw.get("pricing", {}).keys()):
        raw["pricing"][model] = {"input_per_mtok": 1.0, "output_per_mtok": 1.0}
    # Avoid touching the real PCIV checkout from inside CLI tests.
    raw.pop("pciv", None)
    dst_policy.write_text(yaml.safe_dump(raw), encoding="utf-8")


def _read_budget_window_rows(db_path: Path) -> list[tuple[float, int, str | None]]:
    conn = sqlite3.connect(str(db_path))
    try:
        return [
            (float(r[0]), int(r[1]), r[2])
            for r in conn.execute(
                "SELECT amount_usd, forced, note FROM budget_window ORDER BY rowid"
            ).fetchall()
        ]
    finally:
        conn.close()


def _patch_anthropic_adapter(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Patch the Router-imported AnthropicAdapter with a fresh fake."""

    fake = _FakeAdapter()
    monkeypatch.setattr(
        "budgeteer.router.AnthropicAdapter",
        lambda *a, **kw: fake,
    )
    return fake


def test_cross_run_cap_rejects_second_invocation_when_window_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    db_path = tmp_path / "cross_run.db"
    test_policy = tmp_path / "policy.yaml"
    _write_test_policy(POLICY_PATH, test_policy, cap_usd=_MONTHLY_CAP_USD, cross_run_db=db_path)

    runner = CliRunner()

    # --- Invocation 1: should succeed and record actual spend. ---
    _patch_anthropic_adapter(monkeypatch)
    result1 = runner.invoke(
        app,
        [
            "run",
            "mechanical edit",
            "--budget",
            f"{_PER_RUN_BUDGET_USD:.6f}",
            "--force-strategy",
            "single",
            "--policy",
            str(test_policy),
            "--repo",
            str(repo),
        ],
    )
    assert result1.exit_code == 0, result1.stdout + result1.stderr
    payload1 = json.loads(result1.stdout)
    assert payload1["result"]["success"] is True
    assert "cross_run" in payload1
    assert payload1["cross_run"]["cap_usd"] == pytest.approx(_MONTHLY_CAP_USD)
    assert payload1["cross_run"]["spent_usd"] == pytest.approx(0.0)
    # effective_budget_usd should be capped to the cross-run remaining
    # (= cap, since spent is 0 going in).
    assert payload1["cross_run"]["effective_budget_usd"] == pytest.approx(_MONTHLY_CAP_USD)

    rows_after_run_1 = _read_budget_window_rows(db_path)
    assert len(rows_after_run_1) == 1
    amount_1, forced_1, _ = rows_after_run_1[0]
    assert amount_1 == pytest.approx(_PER_RUN_SPEND_USD, rel=0.05)
    assert forced_1 == 0

    # --- Invocation 2: should fail-fast at preflight. ---
    # The remaining window allowance ($5e-5) is less than the policy
    # tight_budget_usd ($0.50), so the router would route to
    # single_agent_fallback regardless. The cross-run preflight short-
    # circuits BEFORE the router runs, so we never see the run execute.
    # We force-exhaust by seeding the ledger up to the cap to make the
    # rejection deterministic regardless of fixture-spend rounding.
    from agentcore.budget import PersistentBudgetLedger

    with PersistentBudgetLedger(db_path, cap_usd=_MONTHLY_CAP_USD, window="monthly") as seed:
        # Top up to exactly the cap so remaining == 0 on next preflight.
        topup = max(0.0, _MONTHLY_CAP_USD - seed.spent_in_current_window())
        if topup > 0:
            seed.charge(topup, note="topup-to-cap")

    _patch_anthropic_adapter(monkeypatch)
    result2 = runner.invoke(
        app,
        [
            "run",
            "another mechanical edit",
            "--budget",
            f"{_PER_RUN_BUDGET_USD:.6f}",
            "--force-strategy",
            "single",
            "--policy",
            str(test_policy),
            "--repo",
            str(repo),
        ],
    )
    assert result2.exit_code == 2, result2.stdout + result2.stderr
    combined = (result2.output or "") + (result2.stderr or "")
    assert "cross-run" in combined.lower(), combined
    # Run 2 must not have written a new row.
    rows_after_run_2 = _read_budget_window_rows(db_path)
    assert len(rows_after_run_2) == len(_read_budget_window_rows(db_path))


def test_ignore_cross_run_cap_overrides_rejection_and_marks_row_forced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    db_path = tmp_path / "cross_run.db"
    test_policy = tmp_path / "policy.yaml"
    _write_test_policy(POLICY_PATH, test_policy, cap_usd=_MONTHLY_CAP_USD, cross_run_db=db_path)

    # Pre-seed the ledger to simulate a prior run that exhausted the cap.
    from agentcore.budget import PersistentBudgetLedger

    db_path.parent.mkdir(parents=True, exist_ok=True)
    with PersistentBudgetLedger(db_path, cap_usd=_MONTHLY_CAP_USD, window="monthly") as seed:
        seed.charge(_MONTHLY_CAP_USD, note="prior-run-seed")

    _patch_anthropic_adapter(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "run",
            "emergency hotfix",
            "--budget",
            f"{_PER_RUN_BUDGET_USD:.6f}",
            "--force-strategy",
            "single",
            "--policy",
            str(test_policy),
            "--repo",
            str(repo),
            "--ignore-cross-run-cap",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["result"]["success"] is True

    rows = _read_budget_window_rows(db_path)
    assert len(rows) == 2
    seed_row, forced_row = rows
    assert seed_row[1] == 0  # not forced
    assert forced_row[1] == 1  # forced=1
    assert forced_row[2] is not None and "ignore-cross-run-cap" in forced_row[2]


def test_cross_run_cap_disabled_when_block_omitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Omitted ``[cross_run]`` block → no ledger opened, no audit table."""

    repo = tmp_path / "repo"
    repo.mkdir()
    db_path = tmp_path / "cross_run.db"  # path; should NOT be created
    test_policy = tmp_path / "policy.yaml"
    _write_test_policy(POLICY_PATH, test_policy, cap_usd=None, cross_run_db=db_path)

    _patch_anthropic_adapter(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "run",
            "no cross-run cap",
            "--budget",
            "1.00",
            "--force-strategy",
            "single",
            "--policy",
            str(test_policy),
            "--repo",
            str(repo),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert "cross_run" not in payload
    # No SQLite file should have been created.
    assert not db_path.exists()
