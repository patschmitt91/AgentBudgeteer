"""Tests for SingleAgentStrategy with a mocked Anthropic adapter."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from budgeteer.adapters.anthropic_adapter import AdapterMessage, AdapterResponse
from budgeteer.budget import BudgetGovernor, load_degradation
from budgeteer.pricing import PricingTable
from budgeteer.strategies.single_agent import SingleAgentStrategy
from budgeteer.types import ExecutionContext, Features, RepoSnapshot

POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "policy.yaml"


@dataclass
class _FakeAdapter:
    """Stand-in for AnthropicAdapter that records calls and returns canned data."""

    text: str = "ok"
    tokens_in: int = 1_000
    tokens_out: int = 500
    latency_ms: int = 120
    calls: list[dict[str, Any]] | None = None

    def get_response(
        self,
        messages: list[AdapterMessage],
        *,
        model: str,
        max_tokens: int,
        system: str | None = None,
        on_text: Any | None = None,
    ) -> AdapterResponse:
        if self.calls is None:
            self.calls = []
        self.calls.append(
            {
                "messages": messages,
                "model": model,
                "max_tokens": max_tokens,
                "system": system,
            }
        )
        return AdapterResponse(
            text=self.text,
            model=model,
            tokens_in=self.tokens_in,
            tokens_out=self.tokens_out,
            latency_ms=self.latency_ms,
        )


def _context() -> ExecutionContext:
    return ExecutionContext(
        budget_remaining=5.0,
        latency_target_seconds=600,
        repo_snapshot=RepoSnapshot(root=Path(".")),
        features=Features(
            estimated_file_count=1,
            cross_file_dependency_score=0.1,
            test_presence=False,
            type_safety_signal=False,
            planning_depth_score=1,
            reasoning_vs_mechanical_score=0.2,
            estimated_input_tokens=2_000,
        ),
    )


def test_single_agent_happy_path() -> None:
    pricing = PricingTable.from_yaml(POLICY_PATH)
    governor = BudgetGovernor(pricing, load_degradation(POLICY_PATH), hard_cap_usd=5.0)
    adapter = _FakeAdapter(text="done", tokens_in=1_000, tokens_out=500)
    strategy = SingleAgentStrategy(
        adapter=adapter,  # type: ignore[arg-type]
        pricing=pricing,
        governor=governor,
        model="anthropic-primary",
    )

    result = strategy.execute("Do the thing", _context())

    assert result.success is True
    assert result.strategy_used == "single"
    assert result.output_text == "done"
    assert len(result.model_trace) == 1
    invocation = result.model_trace[0]
    assert invocation.model == "anthropic-primary"
    assert invocation.tokens_in == 1_000
    assert invocation.tokens_out == 500
    # 1_000 in @ 15/Mtok + 500 out @ 75/Mtok = 0.015 + 0.0375 = 0.0525
    assert result.cost_usd > 0.05
    assert governor.spent == result.cost_usd
    assert adapter.calls is not None and len(adapter.calls) == 1


def test_single_agent_refuses_when_budget_too_small() -> None:
    pricing = PricingTable.from_yaml(POLICY_PATH)
    governor = BudgetGovernor(pricing, load_degradation(POLICY_PATH), hard_cap_usd=0.00001)
    adapter = _FakeAdapter()
    strategy = SingleAgentStrategy(
        adapter=adapter,  # type: ignore[arg-type]
        pricing=pricing,
        governor=governor,
        model="anthropic-primary",
    )

    result = strategy.execute("Do the thing", _context())

    assert result.success is False
    assert result.error is not None
    assert "budget" in result.error


def test_single_agent_reports_adapter_error() -> None:
    class _BoomAdapter:
        def get_response(self, *_: Any, **__: Any) -> AdapterResponse:
            raise RuntimeError("network down")

    pricing = PricingTable.from_yaml(POLICY_PATH)
    governor = BudgetGovernor(pricing, load_degradation(POLICY_PATH), hard_cap_usd=5.0)
    strategy = SingleAgentStrategy(
        adapter=_BoomAdapter(),  # type: ignore[arg-type]
        pricing=pricing,
        governor=governor,
        model="anthropic-primary",
    )

    result = strategy.execute("Do the thing", _context())

    assert result.success is False
    assert result.error is not None
    assert "network down" in result.error


def test_post_call_budget_exceeded_returns_failed_result_not_exception() -> None:
    """Regression: `record_spend` raising mid-execute must not bubble out."""
    pricing = PricingTable.from_yaml(POLICY_PATH)
    # Cap is large enough to clear preflight (which projects 100 in / 200 out)
    # but smaller than the actual post-call spend (1000 in / 5000 out).
    governor = BudgetGovernor(pricing, load_degradation(POLICY_PATH), hard_cap_usd=0.05)
    adapter = _FakeAdapter(text="big", tokens_in=1_000, tokens_out=5_000)
    strategy = SingleAgentStrategy(
        adapter=adapter,  # type: ignore[arg-type]
        pricing=pricing,
        governor=governor,
        model="anthropic-primary",
    )

    result = strategy.execute("Do the thing", _context())

    assert result.success is False
    assert result.error is not None
    assert "post_call_budget_exceeded" in result.error
    # Spend was still recorded (the governor raised AFTER applying it).
    assert governor.spent > 0
