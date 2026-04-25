"""Decision tree routing policy loaded from config/policy.yaml."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from budgeteer.classifier import ClassifierConfig
from budgeteer.types import Features


@dataclass(frozen=True)
class RoutingThresholds:
    large_context_token_threshold: int
    tight_budget_usd: float
    short_latency_seconds: int
    fleet_min_file_count: int
    fleet_max_coupling: float
    pciv_min_reasoning_ratio: float
    pciv_min_planning_depth: int


@dataclass(frozen=True)
class ModelDefaults:
    single_agent_primary: str
    single_agent_fallback: str
    pciv_planner: str
    pciv_implementer: str
    fleet_worker: str


@dataclass(frozen=True)
class RouteDecision:
    strategy: str  # "single", "pciv", "fleet"
    model: str
    reason: str


class Policy:
    """Pure-function decision tree over a Features + budget + latency triple."""

    def __init__(self, thresholds: RoutingThresholds, defaults: ModelDefaults) -> None:
        self._t = thresholds
        self._m = defaults

    @classmethod
    def from_yaml(cls, path: Path) -> Policy:
        with path.open("r", encoding="utf-8") as f:
            raw: dict[str, Any] = yaml.safe_load(f)
        r = raw["routing"]
        thresholds = RoutingThresholds(
            large_context_token_threshold=int(r["large_context_token_threshold"]),
            tight_budget_usd=float(r["tight_budget_usd"]),
            short_latency_seconds=int(r["short_latency_seconds"]),
            fleet_min_file_count=int(r["fleet_min_file_count"]),
            fleet_max_coupling=float(r["fleet_max_coupling"]),
            pciv_min_reasoning_ratio=float(r["pciv_min_reasoning_ratio"]),
            pciv_min_planning_depth=int(r["pciv_min_planning_depth"]),
        )
        m = raw["model_defaults"]
        defaults = ModelDefaults(
            single_agent_primary=str(m["single_agent_primary"]),
            single_agent_fallback=str(m["single_agent_fallback"]),
            pciv_planner=str(m["pciv_planner"]),
            pciv_implementer=str(m["pciv_implementer"]),
            fleet_worker=str(m["fleet_worker"]),
        )
        return cls(thresholds, defaults)

    @staticmethod
    def load_classifier_config(path: Path) -> ClassifierConfig:
        """Load classifier wordlists from the ``classifier`` block of policy.yaml.

        Empty or missing lists fall back to the defaults in classifier.py.
        """
        with path.open("r", encoding="utf-8") as f:
            raw: dict[str, Any] = yaml.safe_load(f) or {}
        block = raw.get("classifier") or {}
        defaults = ClassifierConfig.default()

        def _pick(key: str, fallback: frozenset[str]) -> frozenset[str]:
            items = block.get(key) or []
            if not items:
                return fallback
            return frozenset(str(x).lower() for x in items)

        return ClassifierConfig(
            reasoning_tokens=_pick("reasoning_tokens", defaults.reasoning_tokens),
            mechanical_tokens=_pick("mechanical_tokens", defaults.mechanical_tokens),
            imperative_verbs=_pick("imperative_verbs", defaults.imperative_verbs),
        )

    @property
    def defaults(self) -> ModelDefaults:
        return self._m

    @property
    def thresholds(self) -> RoutingThresholds:
        return self._t

    def route(
        self,
        features: Features,
        budget_remaining: float,
        latency_target_seconds: int,
    ) -> RouteDecision:
        t = self._t
        m = self._m

        # Tight-budget guard runs FIRST so a $0.05 task can never select an
        # Opus-class primary, even on a large-context input. Previously the
        # large-context branch was evaluated first and silently overrode the
        # budget cap. See harden/phase-2 audit item #2.
        if budget_remaining < t.tight_budget_usd:
            return RouteDecision(
                strategy="single",
                model=m.single_agent_fallback,
                reason="tight_budget",
            )

        if features.estimated_input_tokens > t.large_context_token_threshold:
            return RouteDecision(
                strategy="single",
                model=m.single_agent_primary,
                reason="input_tokens_exceed_large_context_threshold",
            )

        if latency_target_seconds < t.short_latency_seconds:
            if (
                features.reasoning_vs_mechanical_score > t.pciv_min_reasoning_ratio
                and features.test_presence
            ):
                return RouteDecision(
                    strategy="pciv",
                    model=m.pciv_planner,
                    reason="short_latency_but_reasoning_heavy_with_tests",
                )
            return RouteDecision(
                strategy="single",
                model=m.single_agent_primary,
                reason="short_latency",
            )

        if (
            features.estimated_file_count >= t.fleet_min_file_count
            and features.cross_file_dependency_score < t.fleet_max_coupling
        ):
            return RouteDecision(
                strategy="fleet",
                model=m.fleet_worker,
                reason="many_independent_files",
            )

        if (
            features.reasoning_vs_mechanical_score > t.pciv_min_reasoning_ratio
            and features.test_presence
        ) or features.planning_depth_score >= t.pciv_min_planning_depth:
            return RouteDecision(
                strategy="pciv",
                model=m.pciv_planner,
                reason="reasoning_heavy_or_deep_plan",
            )

        return RouteDecision(
            strategy="single",
            model=m.single_agent_primary,
            reason="default",
        )
