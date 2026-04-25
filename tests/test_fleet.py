"""Tests for Fleet: ledger, sharding, worker, and end-to-end strategy."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from budgeteer.adapters.anthropic_adapter import AdapterMessage, AdapterResponse
from budgeteer.budget import BudgetGovernor, load_degradation
from budgeteer.fleet.ledger import ShardLedger
from budgeteer.fleet.sharding import plan_shards
from budgeteer.fleet.worktree import TempDirWorktreeManager
from budgeteer.pricing import PricingTable
from budgeteer.strategies.fleet import FleetStrategy
from budgeteer.types import ExecutionContext, Features, RepoSnapshot

POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "policy.yaml"


def _context(budget_remaining: float = 5.0) -> ExecutionContext:
    return ExecutionContext(
        budget_remaining=budget_remaining,
        latency_target_seconds=1_200,
        repo_snapshot=RepoSnapshot(root=Path(".")),
        features=Features(
            estimated_file_count=12,
            cross_file_dependency_score=0.1,
            test_presence=False,
            type_safety_signal=False,
            planning_depth_score=1,
            reasoning_vs_mechanical_score=0.1,
            estimated_input_tokens=10_000,
        ),
    )


@dataclass
class _FakeAdapter:
    text: str = "shard done"
    tokens_in: int = 200
    tokens_out: int = 100
    calls: int = 0

    def get_response(
        self,
        messages: list[AdapterMessage],
        *,
        model: str,
        max_tokens: int,
        system: str | None = None,
        on_text: Any | None = None,
    ) -> AdapterResponse:
        self.calls += 1
        return AdapterResponse(
            text=self.text,
            model=model,
            tokens_in=self.tokens_in,
            tokens_out=self.tokens_out,
            latency_ms=42,
        )


# ---------- sharding ----------


def test_sharding_extracts_one_shard_per_file_path() -> None:
    task = "Update a.py and b.py and c.py"
    shards = plan_shards(task)
    assert len(shards) == 3
    assert all("a.py" in s or "b.py" in s or "c.py" in s for s in shards)


def test_sharding_falls_back_to_sentence_split() -> None:
    task = "Clean up module one. Rewrite module two. Document module three."
    shards = plan_shards(task)
    assert len(shards) == 3


def test_sharding_single_shard_for_short_task() -> None:
    shards = plan_shards("Fix the bug")
    assert shards == ["Fix the bug"]


# ---------- ledger ----------


def test_ledger_claim_is_atomic_across_workers() -> None:
    ledger = ShardLedger(":memory:")
    try:
        ledger.record_run("run1", "task")
        for i in range(3):
            ledger.add_shard(f"run1:{i:03d}", "run1", f"shard {i}")

        claimed = []
        for worker in ("w1", "w2", "w3", "w4"):
            s = ledger.claim_next("run1", worker)
            if s is not None:
                claimed.append(s)

        assert len(claimed) == 3
        assert len({s.shard_id for s in claimed}) == 3
        # Fourth claim gets nothing.
        assert ledger.claim_next("run1", "w4") is None
    finally:
        ledger.close()


def test_ledger_completes_and_lists() -> None:
    ledger = ShardLedger(":memory:")
    try:
        ledger.record_run("r", "t")
        ledger.add_shard("r:000", "r", "s0")
        claimed = ledger.claim_next("r", "w")
        assert claimed is not None
        ledger.complete_shard(
            claimed.shard_id,
            result_text="ok",
            cost_usd=0.01,
            tokens_in=10,
            tokens_out=20,
            worktree_path="/tmp/x",
        )
        shards = ledger.list_shards("r")
        assert len(shards) == 1
        assert shards[0].status == "done"
        assert shards[0].cost_usd == pytest.approx(0.01)
        assert shards[0].tokens_out == 20
    finally:
        ledger.close()


# ---------- strategy ----------


def test_fleet_happy_path_runs_all_shards() -> None:
    pricing = PricingTable.from_yaml(POLICY_PATH)
    governor = BudgetGovernor(pricing, load_degradation(POLICY_PATH), hard_cap_usd=10.0)
    adapter = _FakeAdapter()
    ledger = ShardLedger(":memory:")
    manager = TempDirWorktreeManager()

    strategy = FleetStrategy(
        adapter=adapter,  # type: ignore[arg-type]
        pricing=pricing,
        governor=governor,
        model="anthropic-fallback",
        max_workers=3,
        ledger=ledger,
        worktree_manager=manager,
    )

    task = "Convert configs: a.yaml, b.yaml, c.yaml, d.yaml"
    result = strategy.execute(task, _context())

    assert result.success is True
    assert result.strategy_used == "fleet"
    assert adapter.calls == 4
    assert len(result.model_trace) == 4
    assert result.cost_usd > 0
    assert governor.spent == pytest.approx(result.cost_usd)
    # All shards report done in the ledger.
    shards = ledger.list_shards(run_id=_only_run_id(ledger))
    assert all(s.status == "done" for s in shards)
    ledger.close()


def test_fleet_refuses_when_budget_too_small() -> None:
    pricing = PricingTable.from_yaml(POLICY_PATH)
    governor = BudgetGovernor(pricing, load_degradation(POLICY_PATH), hard_cap_usd=0.00001)
    adapter = _FakeAdapter()
    strategy = FleetStrategy(
        adapter=adapter,  # type: ignore[arg-type]
        pricing=pricing,
        governor=governor,
        model="anthropic-fallback",
        ledger=ShardLedger(":memory:"),
        worktree_manager=TempDirWorktreeManager(),
    )
    result = strategy.execute("Convert a.yaml, b.yaml, c.yaml", _context())
    assert result.success is False
    assert result.error is not None
    assert "budget" in result.error
    assert adapter.calls == 0


def test_fleet_reports_per_shard_failure() -> None:
    pricing = PricingTable.from_yaml(POLICY_PATH)
    governor = BudgetGovernor(pricing, load_degradation(POLICY_PATH), hard_cap_usd=10.0)

    class _FlakyAdapter:
        def __init__(self) -> None:
            self.n = 0

        def get_response(
            self, messages: Any, *, model: str, max_tokens: int, **_: Any
        ) -> AdapterResponse:
            self.n += 1
            if self.n == 2:
                raise RuntimeError("shard 2 upstream failed")
            return AdapterResponse(
                text="ok", model=model, tokens_in=100, tokens_out=50, latency_ms=10
            )

    adapter = _FlakyAdapter()
    strategy = FleetStrategy(
        adapter=adapter,  # type: ignore[arg-type]
        pricing=pricing,
        governor=governor,
        model="anthropic-fallback",
        max_workers=2,
        ledger=ShardLedger(":memory:"),
        worktree_manager=TempDirWorktreeManager(),
    )

    result = strategy.execute("Convert a.yaml, b.yaml, c.yaml", _context())
    assert result.success is False
    assert result.error is not None
    assert "shard 2" in result.error or "upstream" in result.error


def _only_run_id(ledger: ShardLedger) -> str:
    cur = ledger._conn.execute("SELECT run_id FROM runs LIMIT 1")  # type: ignore[attr-defined]
    row = cur.fetchone()
    assert row is not None
    return str(row[0])


def test_fleet_aborts_remaining_shards_when_cap_breached() -> None:
    """Regression: per-shard preflight stops new claims once budget is gone.

    Pre-Phase-2 the fleet only checked the projected-total cap once before
    spawning workers and tallied real cost after every worker returned.
    With `record_spend` raising late in the run, shards continued to
    burn API tokens past the cap.
    """
    pricing = PricingTable.from_yaml(POLICY_PATH)
    # Cap covers ~2 large shards. Per-shard projected (fleet, 12 files) is
    # roughly projection_total / num_shards; once 2 shards record real spend
    # the 3rd worker's preflight must raise BudgetExceeded.
    governor = BudgetGovernor(pricing, load_degradation(POLICY_PATH), hard_cap_usd=0.30)
    big_output_adapter = _FakeAdapter(text="x", tokens_in=200, tokens_out=10_000)
    strategy = FleetStrategy(
        adapter=big_output_adapter,  # type: ignore[arg-type]
        pricing=pricing,
        governor=governor,
        model="anthropic-fallback",
        max_workers=1,  # serialize to make the budget arithmetic deterministic
        ledger=ShardLedger(":memory:"),
        worktree_manager=TempDirWorktreeManager(),
    )
    # 4 yaml files -> 4 shards.
    strategy.execute("Convert a.yaml, b.yaml, c.yaml, d.yaml", _context())
    # The strategy may report success=True if at least one shard completed,
    # but it must NOT have run all 4 shards: per-shard preflight aborted.
    assert big_output_adapter.calls < 4, (
        f"expected per-shard preflight to abort claiming new shards once the "
        f"budget was spent, but adapter was called {big_output_adapter.calls} times"
    )
    # Spend may slightly exceed the cap because the final shard's real cost
    # is only known after it returns; what matters for the regression is
    # that ``calls < 4`` -- the next worker iteration aborted instead of
    # claiming and burning another shard.
