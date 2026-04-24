"""SingleAgent strategy: one Opus 4.7 call with streaming.

Baseline for all comparisons. Emits an OpenTelemetry span, records cost
against the pricing table, and enforces the budget cap before starting.
"""

from __future__ import annotations

import time

from budgeteer.adapters.anthropic_adapter import AdapterMessage, AnthropicAdapter
from budgeteer.budget import BudgetExceeded, BudgetGovernor
from budgeteer.pricing import PricingTable
from budgeteer.strategies.base import Strategy
from budgeteer.telemetry import strategy_span
from budgeteer.types import ExecutionContext, ModelInvocation, StrategyResult


class SingleAgentStrategy(Strategy):
    """One streaming call to Opus 4.7 (or a configured fallback)."""

    name = "single"

    def __init__(
        self,
        adapter: AnthropicAdapter,
        pricing: PricingTable,
        governor: BudgetGovernor,
        model: str,
        max_tokens: int = 4096,
        task_id: str = "single-agent",
    ) -> None:
        self._adapter = adapter
        self._pricing = pricing
        self._governor = governor
        self._model = model
        self._max_tokens = max_tokens
        self._task_id = task_id

    def execute(self, task: str, context: ExecutionContext) -> StrategyResult:
        started = time.perf_counter()

        projection = self._governor.project(
            strategy=self.name,
            features=context.features,
            role_model_plan={"primary": self._model},
        )
        effective_model = projection.model_plan["primary"]

        try:
            self._governor.check_can_start(projection.projected_cost_usd)
        except BudgetExceeded as exc:
            return self._failure(started=started, error=f"budget_exceeded: {exc}")

        messages = [
            AdapterMessage(role="system", content=_SYSTEM_PROMPT),
            AdapterMessage(role="user", content=task),
        ]

        with strategy_span(
            self.name,
            self._task_id,
            model=effective_model,
            budget_remaining=float(context.budget_remaining),
            latency_target_seconds=int(context.latency_target_seconds),
        ) as span:
            try:
                response = self._adapter.get_response(
                    messages,
                    model=effective_model,
                    max_tokens=self._max_tokens,
                )
            except Exception as exc:
                span.set_attribute("error", True)
                span.set_attribute("error.message", str(exc))
                return self._failure(started=started, error=f"adapter_error: {exc}")

            cost = self._pricing.cost(effective_model, response.tokens_in, response.tokens_out)
            self._governor.record_spend(cost)

            span.set_attribute("tokens_in", response.tokens_in)
            span.set_attribute("tokens_out", response.tokens_out)
            span.set_attribute("cost_usd", cost)
            span.set_attribute("latency_ms", response.latency_ms)

            invocation = ModelInvocation(
                model=effective_model,
                role="primary",
                tokens_in=response.tokens_in,
                tokens_out=response.tokens_out,
                cost_usd=cost,
                latency_ms=response.latency_ms,
            )

            return StrategyResult(
                success=True,
                cost_usd=cost,
                latency_seconds=self._elapsed(started),
                artifacts=[],
                strategy_used=self.name,
                model_trace=[invocation],
                output_text=response.text,
            )


_SYSTEM_PROMPT = (
    "You are a senior software engineer. Read the task carefully, reason about "
    "the smallest correct change, and respond with the change and a short "
    "rationale. Prefer precise edits over rewrites."
)
