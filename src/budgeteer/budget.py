"""Budget projection, governance, and degradation rules."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from agentcore.budget import BudgetExceeded as _CoreBudgetExceeded

from budgeteer.pricing import PricingTable
from budgeteer.types import Features


class BudgetExceeded(_CoreBudgetExceeded):
    """AgentBudgeteer alias of :class:`agentcore.budget.BudgetExceeded`.

    Subclasses the shared base so cross-project tooling can catch with the
    common type while AB-internal code keeps the historical name.
    """


@dataclass(frozen=True)
class DegradationRule:
    from_model: str
    to_model: str
    protect_roles: tuple[str, ...]


@dataclass(frozen=True)
class DegradationConfig:
    trigger_ratio: float
    swaps: tuple[DegradationRule, ...]


@dataclass
class CostProjection:
    projected_tokens_in: int
    projected_tokens_out: int
    projected_cost_usd: float
    model_plan: dict[str, str]  # role -> model after any degradation
    degraded: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ProjectionCoefficients:
    """Tunable output-token projections per strategy.

    The governor uses these to estimate cost before execution. Values are
    loaded from the ``projection`` block in ``policy.yaml`` so the
    projection curve is tunable without a code change.
    """

    single_base: int = 1_500
    single_per_planning_step: int = 200
    pciv_multiplier: int = 3
    pciv_per_planning_step: int = 300
    fleet_per_shard: int = 1_500
    fleet_max_shards: int = 16

    @classmethod
    def default(cls) -> ProjectionCoefficients:
        return cls()


def load_projection_coefficients(path: Path) -> ProjectionCoefficients:
    with path.open("r", encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}
    block = raw.get("projection") or {}
    defaults = ProjectionCoefficients.default()
    return ProjectionCoefficients(
        single_base=int(block.get("single_base", defaults.single_base)),
        single_per_planning_step=int(
            block.get("single_per_planning_step", defaults.single_per_planning_step)
        ),
        pciv_multiplier=int(block.get("pciv_multiplier", defaults.pciv_multiplier)),
        pciv_per_planning_step=int(
            block.get("pciv_per_planning_step", defaults.pciv_per_planning_step)
        ),
        fleet_per_shard=int(block.get("fleet_per_shard", defaults.fleet_per_shard)),
        fleet_max_shards=int(block.get("fleet_max_shards", defaults.fleet_max_shards)),
    )


def load_degradation(path: Path) -> DegradationConfig:
    with path.open("r", encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f)
    block = raw.get("degradation", {})
    swaps: list[DegradationRule] = []
    for entry in block.get("swap", []) or []:
        swaps.append(
            DegradationRule(
                from_model=str(entry["from"]),
                to_model=str(entry["to"]),
                protect_roles=tuple(entry.get("protect_roles", []) or []),
            )
        )
    return DegradationConfig(
        trigger_ratio=float(block.get("trigger_ratio", 0.7)),
        swaps=tuple(swaps),
    )


@dataclass(frozen=True)
class CrossRunBudgetConfig:
    """Configuration for the cross-run rolling-window cap (ADR 0005).

    ``cap_usd is None`` disables the cross-run check entirely while
    leaving the per-run governor in place. ``window`` selects the
    rolling bucket (``daily`` or ``monthly``). ``db_path`` is resolved
    relative to ``policy.yaml`` unless absolute; defaults to
    ``.budgeteer/cross_run.db`` next to the policy file.
    """

    cap_usd: float | None
    window: Literal["daily", "monthly"]
    db_path: Path | None


def load_cross_run(path: Path) -> CrossRunBudgetConfig:
    """Parse the ``[cross_run]`` block from ``policy.yaml``.

    The block is optional. An omitted block, or a block with
    ``cap_usd`` unset / null, disables cross-run enforcement.
    """

    with path.open("r", encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}
    block = raw.get("cross_run") or {}
    cap = block.get("cap_usd")
    cap_usd = float(cap) if cap is not None else None
    window_raw = str(block.get("window", "monthly"))
    if window_raw not in ("daily", "monthly"):
        raise ValueError(f"cross_run.window must be 'daily' or 'monthly', got {window_raw!r}")
    window: Literal["daily", "monthly"] = "daily" if window_raw == "daily" else "monthly"
    db_path_raw = block.get("db_path")
    if db_path_raw is None:
        db_path: Path | None = (
            path.parent / ".budgeteer" / "cross_run.db" if cap_usd is not None else None
        )
    else:
        db_candidate = Path(str(db_path_raw))
        db_path = db_candidate if db_candidate.is_absolute() else path.parent / db_candidate
    return CrossRunBudgetConfig(cap_usd=cap_usd, window=window, db_path=db_path)


class BudgetGovernor:
    """Projects cost, applies degradation, and enforces the hard cap."""

    def __init__(
        self,
        pricing: PricingTable,
        degradation: DegradationConfig,
        hard_cap_usd: float,
        projection: ProjectionCoefficients | None = None,
    ) -> None:
        self._pricing = pricing
        self._degradation = degradation
        self._hard_cap = hard_cap_usd
        self._projection = projection or ProjectionCoefficients.default()
        self._spent: float = 0.0
        self._lock = threading.Lock()

    @property
    def spent(self) -> float:
        with self._lock:
            return self._spent

    @property
    def remaining(self) -> float:
        with self._lock:
            return max(0.0, self._hard_cap - self._spent)

    def project(
        self,
        strategy: str,
        features: Features,
        role_model_plan: dict[str, str],
    ) -> CostProjection:
        tokens_in = features.estimated_input_tokens
        tokens_out = _projected_output_tokens(strategy, features, self._projection)

        # First pass at baseline plan.
        projected = _sum_cost(self._pricing, role_model_plan, tokens_in, tokens_out, strategy)

        notes: list[str] = []
        plan = dict(role_model_plan)
        degraded = False

        trigger = self._degradation.trigger_ratio * self.remaining
        if self.remaining > 0.0 and projected > trigger:
            plan, swapped = self._apply_swaps(plan)
            if swapped:
                degraded = True
                notes.append(
                    f"degraded non-protected roles because projected {projected:.4f} > "
                    f"{self._degradation.trigger_ratio:.2f} * remaining {self.remaining:.4f}"
                )
                projected = _sum_cost(self._pricing, plan, tokens_in, tokens_out, strategy)

        return CostProjection(
            projected_tokens_in=tokens_in,
            projected_tokens_out=tokens_out,
            projected_cost_usd=projected,
            model_plan=plan,
            degraded=degraded,
            notes=notes,
        )

    def _apply_swaps(self, plan: dict[str, str]) -> tuple[dict[str, str], bool]:
        new_plan = dict(plan)
        swapped = False
        for rule in self._degradation.swaps:
            for role, model in list(new_plan.items()):
                if model != rule.from_model:
                    continue
                if role in rule.protect_roles:
                    continue
                new_plan[role] = rule.to_model
                swapped = True
        return new_plan, swapped

    def check_can_start(self, projected_cost_usd: float) -> None:
        with self._lock:
            remaining = max(0.0, self._hard_cap - self._spent)
        if remaining <= 0.0:
            raise BudgetExceeded(f"budget already exhausted (spent {self._spent:.4f})")
        if projected_cost_usd > remaining:
            raise BudgetExceeded(
                f"projected {projected_cost_usd:.4f} exceeds remaining {remaining:.4f}"
            )

    def record_spend(self, cost_usd: float) -> None:
        if cost_usd < 0:
            raise ValueError("cost_usd must be non-negative")
        with self._lock:
            self._spent += cost_usd
            over_cap = self._spent > self._hard_cap
        # Emit the metric outside the lock so a slow exporter cannot
        # block other strategies that also hold budget state.
        try:
            from budgeteer.telemetry import budget_usd_spent_total

            budget_usd_spent_total().add(float(cost_usd))
        except Exception:
            # Telemetry must never break accounting.
            pass
        if over_cap:
            raise BudgetExceeded(f"spent {self._spent:.4f} exceeds hard cap {self._hard_cap:.4f}")


def _projected_output_tokens(
    strategy: str, features: Features, coeffs: ProjectionCoefficients
) -> int:
    # Rough heuristic. Strategies emit more output the more planning they do.
    if strategy == "single":
        return coeffs.single_base + coeffs.single_per_planning_step * features.planning_depth_score
    if strategy == "pciv":
        # plan + critique + implement + verify, each a few hundred tokens
        return (
            coeffs.single_base * coeffs.pciv_multiplier
            + coeffs.pciv_per_planning_step * features.planning_depth_score
        )
    if strategy == "fleet":
        return coeffs.fleet_per_shard * max(
            1, min(features.estimated_file_count, coeffs.fleet_max_shards)
        )
    return coeffs.single_base


def _sum_cost(
    pricing: PricingTable,
    plan: dict[str, str],
    tokens_in: int,
    tokens_out: int,
    strategy: str,
) -> float:
    if not plan:
        return 0.0
    share_in = tokens_in // len(plan)
    share_out = tokens_out // len(plan)
    # Fleet fans the same input out to every shard worker, so charge full
    # input per role rather than splitting it. ``tokens_out`` was already
    # projected as the sum across shards in ``_projected_output_tokens`` so
    # we keep it un-divided when the plan is single-role to preserve the
    # per-shard scaling. See harden/phase-2 audit item #4.
    if strategy == "fleet":
        share_in = tokens_in
        share_out = tokens_out
    total = 0.0
    for model in plan.values():
        total += pricing.cost(model, share_in, share_out)
    return total
