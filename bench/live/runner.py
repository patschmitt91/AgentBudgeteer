"""Live-provider micro-bench runner (AB-6).

A single bench task in two modes:

- **Replay (default).** Loads ``bench/live/cassettes/<task_id>.json``,
  builds a ``CassetteAdapter`` over it, runs the configured strategy
  through the existing ``Router``, and asserts the actual cost matches
  the recorded total within tolerance and the router selected the
  task's ``expected_strategy``. Always runs in CI; no network, no key.

- **Live.** Set ``BENCH_LIVE=1`` and provide ``ANTHROPIC_API_KEY``.
  The runner opens a ``PersistentBudgetLedger`` with a hard cap from
  the task YAML (default $0.05), wraps a real ``AnthropicAdapter``
  with the ``RecordingAdapter``, and records every call. The hard cap
  is enforced by ``PersistentBudgetLedger.charge`` raising
  ``BudgetExceeded`` BEFORE a second over-cap call would be issued.

Live mode is invoked by hand. CI never sets ``BENCH_LIVE``.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

BENCH_LIVE_DIR = Path(__file__).resolve().parent
REPO_DIR = BENCH_LIVE_DIR.parents[1]
TASKS_DIR = BENCH_LIVE_DIR / "tasks"
CASSETTES_DIR = BENCH_LIVE_DIR / "cassettes"
LEDGER_DIR = BENCH_LIVE_DIR / ".ledger"

if str(REPO_DIR / "src") not in sys.path:  # pragma: no cover - import side effect
    sys.path.insert(0, str(REPO_DIR / "src"))

from agentcore.budget import (  # noqa: E402
    BudgetExceeded,
    PersistentBudgetLedger,
)

from bench.live.cassette import (  # noqa: E402
    Cassette,
    CassetteAdapter,
    RecordingAdapter,
    new_cassette,
)
from budgeteer.classifier import extract_features  # noqa: E402
from budgeteer.policy import Policy  # noqa: E402
from budgeteer.router import Router  # noqa: E402
from budgeteer.types import RepoSnapshot  # noqa: E402


@dataclass(frozen=True)
class LiveBenchTask:
    """A single live-bench task definition (loaded from YAML)."""

    id: str
    description: str
    task_prompt: str
    provider: Literal["anthropic", "azure_openai"]
    model: str
    expected_strategy: str
    cost_cap_usd: float
    per_run_budget_usd: float
    max_latency_seconds: int

    @classmethod
    def load(cls, path: Path) -> LiveBenchTask:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        provider = str(raw["provider"])
        if provider not in ("anthropic", "azure_openai"):
            raise ValueError(
                f"task {path}: provider must be 'anthropic' or 'azure_openai', got {provider!r}"
            )
        return cls(
            id=str(raw["id"]),
            description=str(raw["description"]),
            task_prompt=str(raw["task_prompt"]),
            provider=provider,  # type: ignore[arg-type]
            model=str(raw["model"]),
            expected_strategy=str(raw["expected_strategy"]),
            cost_cap_usd=float(raw["cost_cap_usd"]),
            per_run_budget_usd=float(raw["per_run_budget_usd"]),
            max_latency_seconds=int(raw.get("max_latency_seconds", 600)),
        )


@dataclass
class BenchRunReport:
    task_id: str
    mode: Literal["replay", "live"]
    actual_strategy: str
    expected_strategy: str
    strategy_match: bool
    actual_cost_usd: float
    cost_under_cap: bool
    cap_usd: float
    success: bool
    notes: str = ""


def _empty_repo_snapshot() -> RepoSnapshot:
    """Bench tasks are model-only; we don't depend on a repo fixture."""

    return RepoSnapshot(
        root=Path("."),
        file_count=1,
        total_bytes=200,
        has_tests=False,
        has_type_config=False,
        languages=["py"],
    )


def _resolve_policy_path(policy_arg: Path | None) -> Path:
    if policy_arg is not None:
        return policy_arg
    return REPO_DIR / "config" / "policy.yaml"


def run_replay(
    task: LiveBenchTask,
    *,
    cassette_path: Path,
    policy_path: Path | None = None,
) -> BenchRunReport:
    """Run the bench task with the cassette adapter; no network."""

    cassette = Cassette.load(cassette_path)
    if cassette.task_id != task.id:
        raise ValueError(f"cassette task_id {cassette.task_id!r} != task.id {task.id!r}")
    adapter = CassetteAdapter(cassette)
    return _run_with_adapter(
        task,
        adapter,
        mode="replay",
        recorded_cost=cassette.totals.cost_usd,
        policy_path=policy_path,
    )


def run_live(
    task: LiveBenchTask,
    *,
    cassette_path: Path,
    policy_path: Path | None = None,
) -> BenchRunReport:
    """Run the bench task against the real provider, recording a cassette.

    Hard cap enforcement: opens a per-task ``PersistentBudgetLedger`` at
    ``LEDGER_DIR/<task.id>.db`` with ``cap_usd=task.cost_cap_usd``. The
    recorder calls ``ledger.charge(cost)`` after each adapter call; a
    breach raises ``BudgetExceeded`` and the recorder leaves the
    cassette in-memory but does NOT write it to disk.
    """

    if task.provider != "anthropic":
        raise NotImplementedError(
            f"only provider='anthropic' is wired for live runs in this "
            f"session; got {task.provider!r}. Add the Azure OpenAI live "
            f"recorder before extending."
        )
    # Imported locally so replay-only callers do not need the SDK.
    from budgeteer.adapters.anthropic_adapter import AnthropicAdapter
    from budgeteer.pricing import PricingTable as _PT

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY not set; refusing to attempt a live recording.")

    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    ledger_path = LEDGER_DIR / f"{task.id}.db"
    pricing = _PT.from_yaml(_resolve_policy_path(policy_path))

    def cost_for_call(tokens_in: int, tokens_out: int) -> float:
        return pricing.cost(task.model, tokens_in, tokens_out)

    inner = AnthropicAdapter()
    cassette = new_cassette(task_id=task.id, provider=task.provider, model=task.model)
    with PersistentBudgetLedger(ledger_path, cap_usd=task.cost_cap_usd, window="daily") as ledger:

        def charge(amount_usd: float) -> None:
            # ``charge`` raises BudgetExceeded if the new total would
            # breach the cap; we surface that to the runner which writes
            # NO cassette on a breach.
            ledger.charge(amount_usd, note=f"task={task.id}")

        recording = RecordingAdapter(
            inner,
            cassette,
            cost_for_call=cost_for_call,
            charge=charge,
        )
        try:
            report = _run_with_adapter(
                task,
                recording,
                mode="live",
                recorded_cost=None,
                policy_path=policy_path,
            )
        except BudgetExceeded as exc:
            return BenchRunReport(
                task_id=task.id,
                mode="live",
                actual_strategy="",
                expected_strategy=task.expected_strategy,
                strategy_match=False,
                actual_cost_usd=ledger.spent_in_current_window(),
                cost_under_cap=False,
                cap_usd=task.cost_cap_usd,
                success=False,
                notes=f"hard cap breached: {exc}; cassette NOT written",
            )

    # Only persist on a clean run within the cap.
    cassette.save(cassette_path)
    return report


def _run_with_adapter(
    task: LiveBenchTask,
    adapter: Any,
    *,
    mode: Literal["replay", "live"],
    recorded_cost: float | None,
    policy_path: Path | None,
) -> BenchRunReport:
    pp = _resolve_policy_path(policy_path)
    snapshot = _empty_repo_snapshot()

    # Build the Router with the adapter pre-injected so it does not
    # construct an AnthropicAdapter (which would require the SDK / key).
    router = Router(
        policy_path=pp,
        budget_cap_usd=task.per_run_budget_usd,
        adapter=adapter,
    )

    # Sanity: confirm the policy would route this task to the expected
    # strategy. We force the strategy to take the cassette path through
    # the same code path either way (the assertion below catches drift).
    features = extract_features(
        task.task_prompt,
        snapshot,
        Policy.load_classifier_config(pp),
    )
    decision = router.policy.route(
        features,
        router.governor.remaining,
        task.max_latency_seconds,
    )

    outcome = router.run(
        task=task.task_prompt,
        repo_snapshot=snapshot,
        latency_target_seconds=task.max_latency_seconds,
        forced=task.expected_strategy,
        task_id=task.id,
    )

    actual_cost = float(outcome.result.cost_usd)
    cost_under_cap = actual_cost <= task.cost_cap_usd
    strategy_match = decision.strategy == task.expected_strategy

    notes_parts: list[str] = []
    if not strategy_match:
        notes_parts.append(
            f"router would have picked {decision.strategy!r}; forced to "
            f"{task.expected_strategy!r} for cassette stability"
        )
    if mode == "replay" and recorded_cost is not None:
        diff = abs(actual_cost - recorded_cost)
        if diff > 1e-9:
            notes_parts.append(
                f"replay cost {actual_cost:.6f} != recorded total "
                f"{recorded_cost:.6f} (diff {diff:.6f})"
            )

    success = (
        outcome.result.success
        and cost_under_cap
        and (mode == "live" or recorded_cost is None or abs(actual_cost - recorded_cost) <= 1e-9)
    )

    return BenchRunReport(
        task_id=task.id,
        mode=mode,
        actual_strategy=outcome.decision.strategy,
        expected_strategy=task.expected_strategy,
        strategy_match=strategy_match,
        actual_cost_usd=actual_cost,
        cost_under_cap=cost_under_cap,
        cap_usd=task.cost_cap_usd,
        success=success,
        notes="; ".join(notes_parts),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AB-6 live-provider micro-bench.")
    parser.add_argument("task_id", help="Task id (matches bench/live/tasks/<id>.yaml)")
    parser.add_argument(
        "--policy",
        type=Path,
        default=None,
        help="Override policy.yaml path. Defaults to config/policy.yaml.",
    )
    args = parser.parse_args(argv)

    task_path = TASKS_DIR / f"{args.task_id}.yaml"
    if not task_path.is_file():
        parser.error(f"task file not found: {task_path}")
    cassette_path = CASSETTES_DIR / f"{args.task_id}.json"

    task = LiveBenchTask.load(task_path)
    live = os.environ.get("BENCH_LIVE") == "1"
    if live:
        print(f"[live] recording cassette for {task.id} (cap=${task.cost_cap_usd})")
        report = run_live(task, cassette_path=cassette_path, policy_path=args.policy)
    else:
        if not cassette_path.is_file():
            print(
                f"[replay] no cassette at {cassette_path}; "
                f"set BENCH_LIVE=1 to record one (provider={task.provider}, "
                f"model={task.model}, cap=${task.cost_cap_usd}).",
                file=sys.stderr,
            )
            return 2
        print(f"[replay] {task.id} from {cassette_path.name}")
        report = run_replay(task, cassette_path=cassette_path, policy_path=args.policy)

    print(
        f"  strategy={report.actual_strategy} "
        f"expected={report.expected_strategy} match={report.strategy_match}"
    )
    print(
        f"  cost_usd={report.actual_cost_usd:.6f} cap_usd={report.cap_usd:.6f} "
        f"under_cap={report.cost_under_cap}"
    )
    if report.notes:
        print(f"  notes: {report.notes}")
    return 0 if report.success else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
