"""Tests for PCIVStrategy. The real pciv workflow is replaced with a fake runner."""

from __future__ import annotations

from pathlib import Path

import pytest

from budgeteer.adapters.pciv_adapter import (
    PCIVCostLine,
    PCIVRunReport,
    PCIVRunRequest,
)
from budgeteer.budget import BudgetGovernor, load_degradation
from budgeteer.pricing import PricingTable
from budgeteer.router import Router
from budgeteer.strategies.pciv import PCIVStrategy
from budgeteer.types import ExecutionContext, Features, RepoSnapshot

POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "policy.yaml"
PCIV_CONFIG_PATH = (POLICY_PATH.parent.parent.parent / "PCIV" / "plan.yaml").resolve()


def _context(budget_remaining: float = 5.0) -> ExecutionContext:
    return ExecutionContext(
        budget_remaining=budget_remaining,
        latency_target_seconds=600,
        repo_snapshot=RepoSnapshot(root=Path(".")),
        features=Features(
            estimated_file_count=3,
            cross_file_dependency_score=0.3,
            test_presence=True,
            type_safety_signal=True,
            planning_depth_score=6,
            reasoning_vs_mechanical_score=0.9,
            estimated_input_tokens=20_000,
        ),
    )


def _fake_report(total_cost: float = 0.05, blocks: bool = False) -> PCIVRunReport:
    return PCIVRunReport(
        run_id="test-run",
        success=not blocks,
        blocks_proceed=blocks,
        plan_goals=["goal-a"],
        plan_subtask_count=2,
        critique_issues=[],
        cost_lines=[
            PCIVCostLine("claude-opus-4-7", 1_000, 500, total_cost * 0.7, "planner_or_verifier"),
            PCIVCostLine("gpt-5.4", 1_200, 300, total_cost * 0.3, "critic"),
        ],
        total_cost_usd=total_cost,
        output_text="fake pciv run",
    )


@pytest.fixture
def pciv_config_file(tmp_path: Path) -> Path:
    # Point at a real file so the strategy's existence check passes; contents
    # are never read because the runner is faked.
    config = tmp_path / "plan.yaml"
    config.write_text("version: 1\n", encoding="utf-8")
    return config


def test_pciv_happy_path(pciv_config_file: Path) -> None:
    pricing = PricingTable.from_yaml(POLICY_PATH)
    governor = BudgetGovernor(pricing, load_degradation(POLICY_PATH), hard_cap_usd=5.0)

    captured: list[PCIVRunRequest] = []

    def fake_runner(req: PCIVRunRequest) -> PCIVRunReport:
        captured.append(req)
        return _fake_report(total_cost=0.08, blocks=False)

    strategy = PCIVStrategy(
        pciv_config_path=pciv_config_file,
        pricing=pricing,
        governor=governor,
        runner=fake_runner,
    )

    result = strategy.execute("Design and implement retries", _context())

    assert result.success is True
    assert result.strategy_used == "pciv"
    assert result.output_text == "fake pciv run"
    assert len(result.model_trace) == 2
    assert result.cost_usd == pytest.approx(0.08)
    assert governor.spent == pytest.approx(0.08)
    assert len(captured) == 1
    assert captured[0].task == "Design and implement retries"
    assert captured[0].config_path == pciv_config_file


def test_pciv_blocked_critique_returns_failure(pciv_config_file: Path) -> None:
    pricing = PricingTable.from_yaml(POLICY_PATH)
    governor = BudgetGovernor(pricing, load_degradation(POLICY_PATH), hard_cap_usd=5.0)

    strategy = PCIVStrategy(
        pciv_config_path=pciv_config_file,
        pricing=pricing,
        governor=governor,
        runner=lambda req: _fake_report(total_cost=0.03, blocks=True),
    )

    result = strategy.execute("Bad plan", _context())

    assert result.success is False
    assert result.cost_usd == pytest.approx(0.03)
    assert governor.spent == pytest.approx(0.03)


def test_pciv_reports_missing_config(tmp_path: Path) -> None:
    pricing = PricingTable.from_yaml(POLICY_PATH)
    governor = BudgetGovernor(pricing, load_degradation(POLICY_PATH), hard_cap_usd=5.0)
    missing = tmp_path / "does_not_exist.yaml"

    strategy = PCIVStrategy(
        pciv_config_path=missing,
        pricing=pricing,
        governor=governor,
        runner=lambda req: _fake_report(),
    )

    result = strategy.execute("Task", _context())
    assert result.success is False
    assert result.error is not None
    assert "pciv_config_missing" in result.error


def test_pciv_reports_runner_exception(pciv_config_file: Path) -> None:
    pricing = PricingTable.from_yaml(POLICY_PATH)
    governor = BudgetGovernor(pricing, load_degradation(POLICY_PATH), hard_cap_usd=5.0)

    def boom(req: PCIVRunRequest) -> PCIVRunReport:
        raise RuntimeError("pciv upstream broke")

    strategy = PCIVStrategy(
        pciv_config_path=pciv_config_file,
        pricing=pricing,
        governor=governor,
        runner=boom,
    )

    result = strategy.execute("Task", _context())
    assert result.success is False
    assert result.error is not None
    assert "pciv upstream broke" in result.error


def test_pciv_refuses_when_budget_too_small(pciv_config_file: Path) -> None:
    pricing = PricingTable.from_yaml(POLICY_PATH)
    # Hard cap tiny so projection exceeds remaining.
    governor = BudgetGovernor(pricing, load_degradation(POLICY_PATH), hard_cap_usd=0.00001)

    called = {"n": 0}

    def fake_runner(req: PCIVRunRequest) -> PCIVRunReport:
        called["n"] += 1
        return _fake_report()

    strategy = PCIVStrategy(
        pciv_config_path=pciv_config_file,
        pricing=pricing,
        governor=governor,
        runner=fake_runner,
    )

    result = strategy.execute("Task", _context())
    assert result.success is False
    assert result.error is not None
    assert "budget" in result.error
    assert called["n"] == 0  # runner never invoked


def test_router_wires_pciv_when_decision_is_pciv(pciv_config_file: Path) -> None:
    runs: list[PCIVRunRequest] = []

    def fake_runner(req: PCIVRunRequest) -> PCIVRunReport:
        runs.append(req)
        return _fake_report(total_cost=0.04)

    router = Router(
        policy_path=POLICY_PATH,
        budget_cap_usd=5.0,
        pciv_config_path=pciv_config_file,
        pciv_runner=fake_runner,
    )

    outcome = router.run(
        task="Design a retry policy. Write a spec. Implement it. Add tests. "
        "Document the rationale. Verify with reviewers.",
        repo_snapshot=RepoSnapshot(
            root=Path("."),
            file_count=3,
            total_bytes=40_000,
            has_tests=True,
            has_type_config=True,
        ),
        latency_target_seconds=600,
        forced="pciv",
    )

    assert outcome.decision.strategy == "pciv"
    assert outcome.result.success is True
    assert len(runs) == 1
