"""Top-level router: classify, apply policy, execute the selected strategy."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from budgeteer.adapters.anthropic_adapter import AnthropicAdapter
from budgeteer.adapters.pciv_adapter import PCIVRunner
from budgeteer.budget import BudgetGovernor, load_degradation, load_projection_coefficients
from budgeteer.classifier import extract_features
from budgeteer.policy import Policy, RouteDecision
from budgeteer.pricing import PricingTable
from budgeteer.strategies.base import Strategy
from budgeteer.strategies.fleet import FleetStrategy
from budgeteer.strategies.pciv import PCIVStrategy
from budgeteer.strategies.single_agent import SingleAgentStrategy
from budgeteer.types import ExecutionContext, Features, RepoSnapshot, StrategyResult


@dataclass
class RouterOutcome:
    decision: RouteDecision
    features: Features
    result: StrategyResult


class Router:
    """Glue between classifier, policy, budget, and strategies."""

    def __init__(
        self,
        policy_path: Path,
        budget_cap_usd: float,
        adapter: AnthropicAdapter | None = None,
        pciv_config_path: Path | None = None,
        pciv_runner: PCIVRunner | None = None,
    ) -> None:
        self._policy_path = policy_path
        self._policy = Policy.from_yaml(policy_path)
        self._pricing = PricingTable.from_yaml(policy_path)
        self._classifier_config = Policy.load_classifier_config(policy_path)
        self._governor = BudgetGovernor(
            pricing=self._pricing,
            degradation=load_degradation(policy_path),
            hard_cap_usd=budget_cap_usd,
            projection=load_projection_coefficients(policy_path),
        )
        self._adapter = adapter
        self._pciv_config_path = pciv_config_path or _resolve_pciv_config(policy_path)
        self._pciv_runner = pciv_runner

    @property
    def policy(self) -> Policy:
        return self._policy

    @property
    def governor(self) -> BudgetGovernor:
        return self._governor

    def route_only(
        self,
        task: str,
        repo_snapshot: RepoSnapshot,
        latency_target_seconds: int,
        forced: str | None = None,
    ) -> tuple[Features, RouteDecision]:
        features = extract_features(task, repo_snapshot, self._classifier_config)
        if forced:
            decision = _forced_decision(forced, self._policy)
        else:
            decision = self._policy.route(
                features,
                self._governor.remaining,
                latency_target_seconds,
            )
        return features, decision

    def run(
        self,
        task: str,
        repo_snapshot: RepoSnapshot,
        latency_target_seconds: int,
        forced: str | None = None,
        task_id: str = "task",
    ) -> RouterOutcome:
        features, decision = self.route_only(task, repo_snapshot, latency_target_seconds, forced)
        context = ExecutionContext(
            budget_remaining=max(self._governor.remaining, 1e-9),
            latency_target_seconds=latency_target_seconds,
            repo_snapshot=repo_snapshot,
            features=features,
        )
        strategy = self._build_strategy(decision, task_id)
        result = strategy.execute(task, context)
        return RouterOutcome(decision=decision, features=features, result=result)

    def _build_strategy(self, decision: RouteDecision, task_id: str) -> Strategy:
        if decision.strategy == "single":
            adapter = self._adapter or AnthropicAdapter()
            return SingleAgentStrategy(
                adapter=adapter,
                pricing=self._pricing,
                governor=self._governor,
                model=decision.model,
                task_id=task_id,
            )
        if decision.strategy == "pciv":
            return PCIVStrategy(
                pciv_config_path=self._pciv_config_path,
                pricing=self._pricing,
                governor=self._governor,
                task_id=task_id,
                runner=self._pciv_runner,
            )
        if decision.strategy == "fleet":
            adapter = self._adapter or AnthropicAdapter()
            fleet_settings = _load_fleet_settings(self._policy_path)
            return FleetStrategy(
                adapter=adapter,
                pricing=self._pricing,
                governor=self._governor,
                model=decision.model,
                max_workers=fleet_settings.get("max_workers", 4),
                per_shard_max_tokens=fleet_settings.get("per_shard_max_tokens", 2048),
                task_id=task_id,
            )
        raise ValueError(f"unknown strategy {decision.strategy!r}")


def _load_policy_block(policy_path: Path, key: str) -> dict[str, Any]:
    with policy_path.open("r", encoding="utf-8") as f:
        raw: dict[str, object] = yaml.safe_load(f) or {}
    block = raw.get(key) or {}
    return block if isinstance(block, dict) else {}


def _load_fleet_settings(policy_path: Path) -> dict[str, int]:
    block = _load_policy_block(policy_path, "fleet")
    out: dict[str, int] = {}
    for key in ("max_workers", "per_shard_max_tokens"):
        val = block.get(key)
        if isinstance(val, int):
            out[key] = val
    return out


def _resolve_pciv_config(policy_path: Path) -> Path:
    pciv_block = _load_policy_block(policy_path, "pciv")
    raw_path = pciv_block.get("config_path")
    if not raw_path:
        return policy_path.parent.parent / "plan.yaml"
    candidate = Path(str(raw_path))
    if candidate.is_absolute():
        return candidate
    return (policy_path.parent / candidate).resolve()


def _forced_decision(forced: str, policy: Policy) -> RouteDecision:
    m = policy.defaults
    if forced == "single":
        return RouteDecision(strategy="single", model=m.single_agent_primary, reason="forced")
    if forced == "pciv":
        return RouteDecision(strategy="pciv", model=m.pciv_planner, reason="forced")
    if forced == "fleet":
        return RouteDecision(strategy="fleet", model=m.fleet_worker, reason="forced")
    raise ValueError(f"unknown forced strategy {forced!r}")
