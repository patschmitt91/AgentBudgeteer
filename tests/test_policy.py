"""Routing assertions for the 10 bench tasks.

Each test constructs an explicit Features vector so the assertion isolates
the policy decision from classifier noise.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from budgeteer.policy import Policy
from budgeteer.types import Features

POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "policy.yaml"


@pytest.fixture(scope="module")
def policy() -> Policy:
    return Policy.from_yaml(POLICY_PATH)


def _features(**overrides: object) -> Features:
    base: dict[str, object] = {
        "estimated_file_count": 1,
        "cross_file_dependency_score": 0.1,
        "test_presence": False,
        "type_safety_signal": False,
        "planning_depth_score": 1,
        "reasoning_vs_mechanical_score": 0.2,
        "estimated_input_tokens": 5_000,
    }
    base.update(overrides)
    return Features(**base)  # type: ignore[arg-type]


def test_task_01_single_file_refactor(policy: Policy) -> None:
    f = _features(estimated_file_count=1, reasoning_vs_mechanical_score=0.2)
    d = policy.route(f, budget_remaining=2.5, latency_target_seconds=600)
    assert d.strategy == "single"


def test_task_02_cross_cutting_rename(policy: Policy) -> None:
    f = _features(
        estimated_file_count=12,
        cross_file_dependency_score=0.8,
        reasoning_vs_mechanical_score=0.0,
        planning_depth_score=2,
    )
    d = policy.route(f, budget_remaining=2.5, latency_target_seconds=600)
    # High coupling blocks Fleet, low reasoning blocks PCIV, default is single.
    assert d.strategy == "single"


def test_task_03_planning_heavy_design(policy: Policy) -> None:
    f = _features(planning_depth_score=7, reasoning_vs_mechanical_score=0.5)
    d = policy.route(f, budget_remaining=5.0, latency_target_seconds=600)
    assert d.strategy == "pciv"


def test_task_04_large_context_audit(policy: Policy) -> None:
    f = _features(estimated_input_tokens=900_000)
    d = policy.route(f, budget_remaining=10.0, latency_target_seconds=600)
    assert d.strategy == "single"
    assert d.reason == "input_tokens_exceed_large_context_threshold"


def test_task_05_independent_parallel_docs(policy: Policy) -> None:
    f = _features(
        estimated_file_count=20,
        cross_file_dependency_score=0.1,
        reasoning_vs_mechanical_score=0.1,
    )
    d = policy.route(f, budget_remaining=5.0, latency_target_seconds=1_200)
    assert d.strategy == "fleet"


def test_task_06_multi_file_feature(policy: Policy) -> None:
    f = _features(
        estimated_file_count=4,
        cross_file_dependency_score=0.5,
        test_presence=True,
        reasoning_vs_mechanical_score=0.8,
        planning_depth_score=3,
    )
    d = policy.route(f, budget_remaining=3.0, latency_target_seconds=600)
    assert d.strategy == "pciv"


def test_task_07_debug_with_tests(policy: Policy) -> None:
    f = _features(
        estimated_file_count=2,
        test_presence=True,
        reasoning_vs_mechanical_score=0.9,
        planning_depth_score=3,
    )
    d = policy.route(f, budget_remaining=2.0, latency_target_seconds=600)
    assert d.strategy == "pciv"


def test_task_08_migration_scripted(policy: Policy) -> None:
    f = _features(
        estimated_file_count=30,
        cross_file_dependency_score=0.1,
        reasoning_vs_mechanical_score=0.0,
    )
    d = policy.route(f, budget_remaining=5.0, latency_target_seconds=1_200)
    assert d.strategy == "fleet"


def test_task_09_architecture_decision(policy: Policy) -> None:
    f = _features(
        planning_depth_score=8,
        reasoning_vs_mechanical_score=0.9,
        test_presence=False,
    )
    d = policy.route(f, budget_remaining=5.0, latency_target_seconds=600)
    assert d.strategy == "pciv"


def test_task_10_tight_budget(policy: Policy) -> None:
    f = _features(
        estimated_file_count=3,
        reasoning_vs_mechanical_score=0.8,
        test_presence=True,
        planning_depth_score=6,
    )
    d = policy.route(f, budget_remaining=0.20, latency_target_seconds=600)
    assert d.strategy == "single"
    assert d.reason == "tight_budget"
    assert d.model == policy.defaults.single_agent_fallback


def test_short_latency_reasoning_heavy_still_pciv(policy: Policy) -> None:
    f = _features(reasoning_vs_mechanical_score=0.9, test_presence=True)
    d = policy.route(f, budget_remaining=2.0, latency_target_seconds=60)
    assert d.strategy == "pciv"


def test_short_latency_default_single(policy: Policy) -> None:
    f = _features(reasoning_vs_mechanical_score=0.1, test_presence=False)
    d = policy.route(f, budget_remaining=2.0, latency_target_seconds=60)
    assert d.strategy == "single"


def test_tight_budget_overrides_large_context(policy: Policy) -> None:
    """Regression: tight-budget guard runs before the large-context branch.

    Pre-Phase-2 the large-context check was evaluated first and silently
    routed a $0.05 task to the (expensive) primary model.
    """
    f = _features(
        estimated_input_tokens=10_000_000,  # well past large_context threshold
        reasoning_vs_mechanical_score=0.5,
    )
    d = policy.route(f, budget_remaining=0.05, latency_target_seconds=600)
    assert d.strategy == "single"
    assert d.reason == "tight_budget"
    assert d.model == policy.defaults.single_agent_fallback
