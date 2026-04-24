"""Strategy abstract base. Every strategy exposes the same interface."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import ClassVar

from budgeteer.types import ExecutionContext, ModelInvocation, StrategyResult


class Strategy(ABC):
    """Uniform interface over SingleAgent, PCIV, and Fleet."""

    name: ClassVar[str] = "abstract"

    @abstractmethod
    def execute(self, task: str, context: ExecutionContext) -> StrategyResult:
        """Run the task and return a uniform result."""

    # ---- shared helpers used by concrete strategies -------------------

    def _elapsed(self, started: float) -> float:
        return time.perf_counter() - started

    def _failure(
        self,
        *,
        started: float,
        error: str,
        cost_usd: float = 0.0,
        trace: list[ModelInvocation] | None = None,
    ) -> StrategyResult:
        """Uniform failure result. Keeps error-branch code in one place."""
        return StrategyResult(
            success=False,
            cost_usd=cost_usd,
            latency_seconds=self._elapsed(started),
            strategy_used=self.name,
            model_trace=list(trace or []),
            error=error,
        )
