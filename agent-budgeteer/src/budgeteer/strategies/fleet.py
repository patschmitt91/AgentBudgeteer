"""Fleet strategy: N parallel workers against a shared SQLite ledger.

Each shard becomes one model call in a dedicated git worktree (or a
tempdir when the repo is not a git repo). The ledger is the single
source of truth; on retry we can resume from the last unfinished shard.
"""

from __future__ import annotations

import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor

from budgeteer.adapters.anthropic_adapter import AnthropicAdapter
from budgeteer.budget import BudgetExceeded, BudgetGovernor
from budgeteer.fleet.ledger import ShardLedger
from budgeteer.fleet.sharding import plan_shards
from budgeteer.fleet.worker import Worker, WorkerResult
from budgeteer.fleet.worktree import GitWorktreeManager, WorktreeManager
from budgeteer.pricing import PricingTable
from budgeteer.strategies.base import Strategy
from budgeteer.telemetry import strategy_span
from budgeteer.types import ExecutionContext, ModelInvocation, StrategyResult

__all__ = ["FleetStrategy"]


class FleetStrategy(Strategy):
    """Parallel worker fleet coordinated through a shared shard ledger."""

    name = "fleet"

    def __init__(
        self,
        adapter: AnthropicAdapter,
        pricing: PricingTable,
        governor: BudgetGovernor,
        model: str = "claude-sonnet-4-6",
        max_workers: int = 4,
        per_shard_max_tokens: int = 2048,
        ledger: ShardLedger | None = None,
        worktree_manager: WorktreeManager | None = None,
        task_id: str = "fleet",
    ) -> None:
        self._adapter = adapter
        self._pricing = pricing
        self._governor = governor
        self._model = model
        self._max_workers = max(1, max_workers)
        self._max_tokens = per_shard_max_tokens
        self._task_id = task_id

        self._ledger = ledger if ledger is not None else ShardLedger(":memory:")
        self._owns_ledger = ledger is None
        self._worktree_manager = worktree_manager

    def execute(self, task: str, context: ExecutionContext) -> StrategyResult:
        started = time.perf_counter()
        try:
            return self._execute(task, context, started)
        finally:
            if self._owns_ledger:
                self._ledger.close()

    def _execute(self, task: str, context: ExecutionContext, started: float) -> StrategyResult:
        shards = plan_shards(task)
        projected_model = self._model
        projection = self._governor.project(
            strategy=self.name,
            features=context.features,
            role_model_plan={"worker": projected_model},
        )
        effective_model = projection.model_plan["worker"]

        try:
            self._governor.check_can_start(projection.projected_cost_usd)
        except BudgetExceeded as exc:
            return self._failure(started=started, error=f"budget_exceeded: {exc}")

        manager = self._worktree_manager or GitWorktreeManager(repo_root=context.repo_snapshot.root)

        run_id = f"fleet-{uuid.uuid4().hex[:12]}"
        self._ledger.record_run(run_id, task)
        for i, description in enumerate(shards):
            self._ledger.add_shard(f"{run_id}:{i:03d}", run_id, description)

        with strategy_span(
            self.name,
            self._task_id,
            model=effective_model,
            shard_count=len(shards),
            max_workers=self._max_workers,
            budget_remaining=float(context.budget_remaining),
            latency_target_seconds=int(context.latency_target_seconds),
        ) as span:
            results = self._run_workers(run_id, manager, effective_model)

            total_cost = sum(r.cost_usd for r in results)
            try:
                if total_cost > 0:
                    self._governor.record_spend(total_cost)
            except BudgetExceeded as exc:
                span.set_attribute("error", True)
                span.set_attribute("error.message", str(exc))
                self._ledger.finalize_run(run_id, status="budget_exceeded")
                return self._failure(
                    started=started,
                    error=f"post_run_budget_exceeded: {exc}",
                    cost_usd=total_cost,
                    trace=_trace_for(results, effective_model),
                )

            failures = [r for r in results if not r.success]
            status = "done" if not failures else "partial"
            self._ledger.finalize_run(run_id, status=status)

            span.set_attribute("cost_usd", total_cost)
            span.set_attribute("fleet_run_id", run_id)
            span.set_attribute("fleet_failures", len(failures))

            trace = _trace_for(results, effective_model)

            if failures:
                first_error = failures[0].error or "unknown_shard_error"
                return StrategyResult(
                    success=False,
                    cost_usd=total_cost,
                    latency_seconds=self._elapsed(started),
                    artifacts=[],
                    strategy_used=self.name,
                    model_trace=trace,
                    error=first_error,
                )

            combined_output = "\n\n".join(f"# shard {r.shard_id}\n{r.output_text}" for r in results)
            return StrategyResult(
                success=True,
                cost_usd=total_cost,
                latency_seconds=self._elapsed(started),
                artifacts=[],
                strategy_used=self.name,
                model_trace=trace,
                output_text=combined_output,
            )

    def _run_workers(
        self,
        run_id: str,
        manager: WorktreeManager,
        model: str,
    ) -> list[WorkerResult]:
        def _one_worker(worker_id: str) -> list[WorkerResult]:
            local: list[WorkerResult] = []
            worker = Worker(
                worker_id=worker_id,
                adapter=self._adapter,
                pricing=self._pricing,
                model=model,
                max_tokens=self._max_tokens,
                ledger=self._ledger,
            )
            while True:
                shard = self._ledger.claim_next(run_id, worker_id)
                if shard is None:
                    return local
                path = manager.provision(run_id, worker_id)
                try:
                    local.append(worker.run_shard(shard, worktree_path=str(path)))
                finally:
                    # Single owner of the worktree lifecycle. No outer cleanup.
                    try:
                        manager.cleanup(path)
                    except Exception:  # noqa: BLE001 - cleanup must not mask shard errors
                        pass

        results: list[WorkerResult] = []
        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures: list[Future[list[WorkerResult]]] = [
                pool.submit(_one_worker, f"w{i}") for i in range(self._max_workers)
            ]
            for fut in futures:
                results.extend(fut.result())
        return results


def _trace_for(results: list[WorkerResult], model: str) -> list[ModelInvocation]:
    return [
        ModelInvocation(
            model=model,
            role="worker",
            tokens_in=r.tokens_in,
            tokens_out=r.tokens_out,
            cost_usd=r.cost_usd,
        )
        for r in results
    ]
