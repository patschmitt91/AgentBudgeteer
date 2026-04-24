"""Tests for pricing, projection, and the budget governor."""

from __future__ import annotations

from pathlib import Path

import pytest

from budgeteer.budget import BudgetExceeded, BudgetGovernor, load_degradation
from budgeteer.pricing import PricingTable
from budgeteer.types import Features

POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "policy.yaml"


def _features(**overrides: object) -> Features:
    base: dict[str, object] = {
        "estimated_file_count": 3,
        "cross_file_dependency_score": 0.3,
        "test_presence": True,
        "type_safety_signal": True,
        "planning_depth_score": 2,
        "reasoning_vs_mechanical_score": 0.5,
        "estimated_input_tokens": 20_000,
    }
    base.update(overrides)
    return Features(**base)  # type: ignore[arg-type]


def test_pricing_round_trip() -> None:
    table = PricingTable.from_yaml(POLICY_PATH)
    assert table.has("anthropic-primary")
    cost = table.cost("anthropic-primary", tokens_in=1_000_000, tokens_out=0)
    assert cost == pytest.approx(15.00)
    cost2 = table.cost("anthropic-fallback", tokens_in=0, tokens_out=1_000_000)
    assert cost2 == pytest.approx(15.00)


def test_pricing_unknown_model_raises() -> None:
    table = PricingTable.from_yaml(POLICY_PATH)
    with pytest.raises(KeyError):
        table.cost("no-such-model", 1, 1)


def test_governor_records_spend_and_tracks_remaining() -> None:
    table = PricingTable.from_yaml(POLICY_PATH)
    gov = BudgetGovernor(table, load_degradation(POLICY_PATH), hard_cap_usd=1.00)
    gov.record_spend(0.40)
    assert gov.spent == pytest.approx(0.40)
    assert gov.remaining == pytest.approx(0.60)


def test_governor_enforces_hard_cap() -> None:
    table = PricingTable.from_yaml(POLICY_PATH)
    gov = BudgetGovernor(table, load_degradation(POLICY_PATH), hard_cap_usd=0.50)
    with pytest.raises(BudgetExceeded):
        gov.record_spend(0.75)


def test_projection_triggers_degradation() -> None:
    table = PricingTable.from_yaml(POLICY_PATH)
    gov = BudgetGovernor(table, load_degradation(POLICY_PATH), hard_cap_usd=0.05)
    features = _features(estimated_input_tokens=50_000)
    projection = gov.project(
        strategy="pciv",
        features=features,
        role_model_plan={
            "planner": "anthropic-primary",
            "critic": "anthropic-primary",
            "implementer": "anthropic-primary",
            "verifier": "anthropic-primary",
        },
    )
    assert projection.degraded is True
    # protected roles stay on opus
    assert projection.model_plan["planner"] == "anthropic-primary"
    assert projection.model_plan["critic"] == "anthropic-primary"
    # non-protected roles get swapped down
    assert projection.model_plan["implementer"] == "anthropic-fallback"
    assert projection.model_plan["verifier"] == "anthropic-fallback"


def test_projection_no_degradation_when_cheap() -> None:
    table = PricingTable.from_yaml(POLICY_PATH)
    gov = BudgetGovernor(table, load_degradation(POLICY_PATH), hard_cap_usd=100.0)
    features = _features(estimated_input_tokens=1_000)
    projection = gov.project(
        strategy="single",
        features=features,
        role_model_plan={"primary": "anthropic-primary"},
    )
    assert projection.degraded is False
    assert projection.model_plan["primary"] == "anthropic-primary"


def test_check_can_start_rejects_overrun() -> None:
    table = PricingTable.from_yaml(POLICY_PATH)
    gov = BudgetGovernor(table, load_degradation(POLICY_PATH), hard_cap_usd=0.10)
    with pytest.raises(BudgetExceeded):
        gov.check_can_start(projected_cost_usd=0.25)
