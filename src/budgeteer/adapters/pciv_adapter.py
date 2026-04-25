"""Adapter over the local `pciv` project.

PCIV owns its own workflow graph, ledger, budget governor, and agents.
The Budgeteer PCIVStrategy needs a single synchronous entry point that
takes a task plus a budget ceiling and returns a structured result. This
module provides that entry point so the strategy can be unit-tested
without monkey-patching pciv internals.

The boundary is a pure function type:

    PCIVRunner = Callable[[PCIVRunRequest], PCIVRunReport]

A real runner is built by `build_default_runner()`. Tests inject their
own callable.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PCIVRunRequest:
    task: str
    repo_path: str
    config_path: Path
    ceiling_usd: float
    # ``True`` only when the operator has explicitly opted in (via the
    # ``--auto-approve-pciv-gates`` CLI flag). HITL gates default to
    # rejection so an unattended run cannot silently merge model output.
    # See harden/phase-2 audit item #6.
    auto_approve_gates: bool = False


@dataclass(frozen=True)
class PCIVCostLine:
    model_id: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    role: str = "unknown"


@dataclass
class PCIVRunReport:
    run_id: str
    success: bool
    blocks_proceed: bool
    plan_goals: list[str] = field(default_factory=list)
    plan_subtask_count: int = 0
    critique_issues: list[str] = field(default_factory=list)
    cost_lines: list[PCIVCostLine] = field(default_factory=list)
    total_cost_usd: float = 0.0
    output_text: str = ""
    error: str | None = None


PCIVRunner = Callable[[PCIVRunRequest], PCIVRunReport]


_ROLE_BY_MODEL: dict[str, str] = {
    # Current PCIV plan.yaml (Azure OpenAI only).
    "azure-reasoning": "planner_or_critic_or_verifier",
    "azure-codegen": "implementer",
    # Retained so legacy fixtures and bench tests still decode role strings.
    "anthropic-primary": "planner_or_verifier",
    "anthropic-fallback": "implementer",
}

# PCIV RunOutcome.status values that indicate a successful merge/ship.
# Mirrors pciv.cli._SUCCESS_STATUSES. Kept here so Budgeteer does not need
# to reach into a private attribute of the pciv CLI module.
_PCIV_SUCCESS_STATUSES: frozenset[str] = frozenset({"merged", "ship"})


def _role_for(model_id: str) -> str:
    return _ROLE_BY_MODEL.get(model_id, "unknown")


async def _auto_approve_gate(_name: str, _payload: dict[str, Any]) -> str:
    """Approve every HITL gate. Used only when the operator opts in."""
    return "approve"


async def _reject_gate(name: str, _payload: dict[str, Any]) -> str:
    """Reject every HITL gate. Default for unattended Budgeteer runs."""
    import logging  # noqa: PLC0415

    logging.getLogger(__name__).info(
        "pciv gate %r rejected: auto-approve not enabled (pass --auto-approve-pciv-gates)",
        name,
    )
    return "reject"


def build_default_runner() -> PCIVRunner:
    """Return a runner that drives the real pciv Pipeline.

    Imported lazily so Budgeteer stays importable even when the pciv package
    is missing (tests inject a fake runner and never hit these imports).
    """

    from pciv.budget import BudgetExceededError, BudgetGovernor
    from pciv.config import load_config
    from pciv.state import Ledger
    from pciv.telemetry import setup_tracing
    from pciv.workflow import Pipeline, cleanup_worktrees

    def _run(req: PCIVRunRequest) -> PCIVRunReport:
        run_id = str(uuid.uuid4())
        try:
            cfg = load_config(req.config_path)
        except FileNotFoundError as exc:
            return PCIVRunReport(
                run_id=run_id,
                success=False,
                blocks_proceed=True,
                error=f"pciv_config_missing: {exc}",
            )

        Path(cfg.runtime.state_dir).mkdir(parents=True, exist_ok=True)
        tracer = setup_tracing(
            service_name=cfg.telemetry.service_name,
            conn_string_env=cfg.telemetry.app_insights_connection_string_env,
        )

        try:
            governor = BudgetGovernor(ceiling_usd=req.ceiling_usd, cfg=cfg)
            governor.preflight()
        except BudgetExceededError as exc:
            return PCIVRunReport(
                run_id=run_id,
                success=False,
                blocks_proceed=True,
                error=f"pciv_budget_exceeded: {exc}",
            )

        max_iter = cfg.iteration.max_rounds
        outcome: Any = None
        try:
            with Ledger(cfg.runtime.sqlite_path) as ledger:
                ledger.record_run(run_id, req.task, req.ceiling_usd, max_iter)
                pipeline = Pipeline(
                    cfg=cfg,
                    governor=governor,
                    ledger=ledger,
                    run_id=run_id,
                    tracer=tracer,
                    repo=Path(req.repo_path),
                    gate_cb=(_auto_approve_gate if req.auto_approve_gates else _reject_gate),
                )
                outcome = asyncio.run(pipeline.run(task=req.task, max_iter=max_iter))
                ledger.finalize_run(run_id, status=outcome.status)
                if outcome.worktrees:
                    cleanup_worktrees(Path(req.repo_path), outcome.worktrees)
        except Exception as exc:  # pragma: no cover - real-world only
            return PCIVRunReport(
                run_id=run_id,
                success=False,
                blocks_proceed=True,
                cost_lines=_lines_from(governor),
                total_cost_usd=governor.spent_usd,
                error=f"pciv_runtime_error: {exc}",
            )

        return _report_from_outcome(run_id, outcome, governor)

    return _run


def _report_from_outcome(run_id: str, outcome: Any, governor: Any) -> PCIVRunReport:
    plan = outcome.plan
    critique = outcome.critique
    success = outcome.status in _PCIV_SUCCESS_STATUSES
    blocks_proceed = bool(critique.blocks_proceed) if critique is not None else True
    return PCIVRunReport(
        run_id=run_id,
        success=success,
        blocks_proceed=blocks_proceed,
        plan_goals=list(plan.goals) if plan is not None else [],
        plan_subtask_count=len(plan.subtasks) if plan is not None else 0,
        critique_issues=list(critique.issues) if critique is not None else [],
        cost_lines=_lines_from(governor),
        total_cost_usd=governor.spent_usd,
        output_text=_summarize(outcome),
        error=None if success else f"pciv_status={outcome.status}: {outcome.message}",
    )


def _lines_from(governor: Any) -> list[PCIVCostLine]:
    return [
        PCIVCostLine(
            model_id=line.model_id,
            input_tokens=line.input_tokens,
            output_tokens=line.output_tokens,
            cost_usd=line.cost_usd,
            role=_role_for(line.model_id),
        )
        for line in governor.lines()
    ]


def _summarize(outcome: Any) -> str:
    plan = outcome.plan
    verdict = outcome.verdict
    goals = "; ".join(plan.goals) if plan is not None else ""
    subtask_ids = ", ".join(s.id for s in plan.subtasks) if plan is not None else ""
    verdict_str = verdict.verdict if verdict is not None else "none"
    merge = outcome.merge
    merged_info = (
        f" merged={merge.merged_tasks} skipped={merge.skipped_tasks}" if merge is not None else ""
    )
    return (
        f"status={outcome.status} verdict={verdict_str} "
        f"iterations={outcome.iterations_used}\n"
        f"goals=[{goals}]\nsubtasks=[{subtask_ids}]{merged_info}"
    )
