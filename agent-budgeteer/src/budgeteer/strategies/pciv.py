"""PCIV strategy: plan, critique, implement, verify.

Delegates to the local `pciv` project through `adapters.pciv_adapter`. The
pciv workflow owns its own governor, ledger, and telemetry. This strategy
translates the pciv run report into Budgeteer's uniform StrategyResult and
charges the actual spend against the Budgeteer budget governor.

pciv phases 3 (Implement) and 4 (Verify) are still marked TODO inside the
pciv project itself. Budgeteer treats a phase-2 pass (critique not
blocking) as success for v0 so the two projects can evolve in lockstep.
"""

from __future__ import annotations

import time
from pathlib import Path

from budgeteer.adapters.pciv_adapter import (
    PCIVRunner,
    PCIVRunRequest,
    build_default_runner,
)
from budgeteer.budget import BudgetExceeded, BudgetGovernor
from budgeteer.pricing import PricingTable
from budgeteer.strategies.base import Strategy
from budgeteer.telemetry import strategy_span
from budgeteer.types import ExecutionContext, ModelInvocation, StrategyResult


class PCIVStrategy(Strategy):
    """Wrapper around the pciv graph workflow."""

    name = "pciv"

    def __init__(
        self,
        pciv_config_path: Path,
        pricing: PricingTable,
        governor: BudgetGovernor,
        task_id: str = "pciv",
        runner: PCIVRunner | None = None,
    ) -> None:
        self._config_path = pciv_config_path
        self._pricing = pricing
        self._governor = governor
        self._task_id = task_id
        self._runner = runner or build_default_runner()

    def execute(self, task: str, context: ExecutionContext) -> StrategyResult:
        started = time.perf_counter()

        planner, critic, implementer, verifier = self._resolve_models()

        projection = self._governor.project(
            strategy=self.name,
            features=context.features,
            role_model_plan={
                "planner": planner,
                "critic": critic,
                "implementer": implementer,
                "verifier": verifier,
            },
        )

        try:
            self._governor.check_can_start(projection.projected_cost_usd)
        except BudgetExceeded as exc:
            return self._failure(started=started, error=f"budget_exceeded: {exc}")

        if not self._config_path.is_file():
            return self._failure(
                started=started,
                error=f"pciv_config_missing: {self._config_path}",
            )

        request = PCIVRunRequest(
            task=task,
            repo_path=str(context.repo_snapshot.root),
            config_path=self._config_path,
            ceiling_usd=max(self._governor.remaining, 1e-6),
        )

        with strategy_span(
            self.name,
            self._task_id,
            pciv_config=str(self._config_path),
            budget_remaining=float(context.budget_remaining),
            latency_target_seconds=int(context.latency_target_seconds),
        ) as span:
            try:
                report = self._runner(request)
            except Exception as exc:
                span.set_attribute("error", True)
                span.set_attribute("error.message", str(exc))
                return self._failure(started=started, error=f"pciv_adapter_error: {exc}")

            trace: list[ModelInvocation] = []
            total_cost = 0.0
            for line in report.cost_lines:
                trace.append(
                    ModelInvocation(
                        model=line.model_id,
                        role=line.role,
                        tokens_in=line.input_tokens,
                        tokens_out=line.output_tokens,
                        cost_usd=line.cost_usd,
                    )
                )
                total_cost += line.cost_usd

            if total_cost > 0:
                try:
                    self._governor.record_spend(total_cost)
                except BudgetExceeded as exc:
                    span.set_attribute("error", True)
                    span.set_attribute("error.message", str(exc))
                    return self._failure(
                        started=started,
                        error=f"post_run_budget_exceeded: {exc}",
                        cost_usd=total_cost,
                        trace=trace,
                    )

            span.set_attribute("cost_usd", total_cost)
            span.set_attribute("pciv_run_id", report.run_id)
            span.set_attribute("pciv_blocks_proceed", report.blocks_proceed)
            span.set_attribute("pciv_subtask_count", report.plan_subtask_count)

            return StrategyResult(
                success=report.success,
                cost_usd=total_cost,
                latency_seconds=self._elapsed(started),
                artifacts=[],
                strategy_used=self.name,
                model_trace=trace,
                output_text=report.output_text,
                error=report.error,
            )

    def _resolve_models(self) -> tuple[str, str, str, str]:
        """Read model IDs from the PCIV plan.yaml config.

        Falls back to hardcoded defaults if pciv is not installed or the
        config file is absent/unparseable. This keeps Budgeteer importable
        without pciv while ensuring cost projections stay in sync with
        whatever models plan.yaml currently declares.
        """
        _DEFAULTS = ("gpt-5.4", "gpt-5.4", "gpt-5.3-codex", "gpt-5.4")
        if not self._config_path.is_file():
            return _DEFAULTS
        try:
            from pciv.config import load_config  # noqa: PLC0415

            cfg = load_config(self._config_path)
            return (
                cfg.models.planner.model_id(),
                cfg.models.critic.model_id(),
                cfg.models.implementer.model_id(),
                cfg.models.verifier.model_id(),
            )
        except Exception:
            return _DEFAULTS
