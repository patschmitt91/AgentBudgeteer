"""Four-arm SWE-bench Verified harness for the v0.3 milestone.

**Status: STUB-ONLY.** This harness runs end-to-end through deterministic
stub adapters. It does **not** call any model, clone any repo, or execute
the SWE-bench evaluation Docker harness. Its purpose is to demonstrate
that the four-arm comparison infrastructure is wired correctly so an
Agent Framework engineer can take it, plug live providers and the SWE-bench
evaluation Docker pipeline in, and run the actual benchmark.

What this harness DOES:

- Loads ``instances_v0_3.txt`` (committed, immutable).
- For each instance, runs the configured task through all four arms
  (``router``, ``single``, ``pciv``, ``fleet``) via the existing
  ``budgeteer.router.Router`` with ``forced=`` for the control arms.
- Injects deterministic stub adapters so no model is ever called and
  cost is effectively zero (a few hundredths of a cent from projection
  rounding).
- Emits a well-formed ``results.json`` plus a ``manifest.json``.
- Maps strategy errors to a five-bucket failure-mode histogram.

What this harness does NOT do (per ``docs/plans/v0.3-handoff-brief.md``):

- Load real ``problem_statement`` text from
  ``princeton-nlp/SWE-bench_Verified``. In stub mode, the task is
  ``"resolve {instance_id}"``. Real runs need ``datasets`` and a
  ``--problems-file`` flag.
- Clone the repo, install deps, or run the SWE-bench evaluation
  Docker harness. Those steps live in ``PCIV/scripts/swe_bench_run.py``
  and need to be lifted over (with citation) when the harness goes
  live.
- Record cassettes. Replay-from-cassette is the v0.3 plan's
  reproducibility story; ``bench/live/cassette.py`` only supports
  Anthropic today (``run_live`` raises ``NotImplementedError`` for
  ``provider=\"azure_openai\"``). Adding the Azure OpenAI cassette
  recorder is one of the explicit handoff TODOs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BENCH_SWE_DIR = Path(__file__).resolve().parent
REPO_DIR = BENCH_SWE_DIR.parents[1]

if str(REPO_DIR / "src") not in sys.path:  # pragma: no cover - import side effect
    sys.path.insert(0, str(REPO_DIR / "src"))

from budgeteer.adapters.anthropic_adapter import (  # noqa: E402
    AdapterMessage,
    AdapterResponse,
    OnTextCallback,
)
from budgeteer.adapters.pciv_adapter import (  # noqa: E402
    PCIVCostLine,
    PCIVRunner,
    PCIVRunReport,
    PCIVRunRequest,
)
from budgeteer.router import Router  # noqa: E402
from budgeteer.types import RepoSnapshot, StrategyResult  # noqa: E402

ARMS: tuple[str, ...] = ("router", "single", "pciv", "fleet")
DEFAULT_INSTANCES_FILE = BENCH_SWE_DIR / "instances_v0_3.txt"
DEFAULT_POLICY_PATH = REPO_DIR / "config" / "policy.yaml"


# ---------------------------------------------------------------------------
# Stub adapters (no spend, deterministic)
# ---------------------------------------------------------------------------


class _StubAdapter:
    """Satisfies ``StreamingChatClient`` Protocol; returns canned tokens.

    Token counts (100 in / 50 out) are intentionally low so projection
    coefficients in ``policy.yaml`` clear the per-run budget for any
    reasonable cap. Cost at azure-codegen rates: ~$0.000375 per call.
    """

    def __init__(self, *, tokens_in: int = 100, tokens_out: int = 50) -> None:
        self._tokens_in = tokens_in
        self._tokens_out = tokens_out

    def get_response(
        self,
        messages: list[AdapterMessage],
        *,
        model: str,
        max_tokens: int,
        system: str | None = None,
        on_text: OnTextCallback | None = None,
    ) -> AdapterResponse:
        text = f"[stub response for {model}]"
        if on_text is not None:
            on_text(text)
        return AdapterResponse(
            text=text,
            model=model,
            tokens_in=self._tokens_in,
            tokens_out=self._tokens_out,
            latency_ms=1,
        )


def _stub_pciv_runner(req: PCIVRunRequest) -> PCIVRunReport:
    """Deterministic PCIVRunner stub. No subprocess, no model calls."""

    return PCIVRunReport(
        run_id=f"stub-{uuid.uuid4().hex[:8]}",
        success=True,
        blocks_proceed=False,
        plan_goals=["stub goal 1", "stub goal 2"],
        plan_subtask_count=2,
        critique_issues=[],
        cost_lines=[
            PCIVCostLine(
                model_id="azure-reasoning",
                input_tokens=100,
                output_tokens=50,
                cost_usd=0.00075,
                role="planner",
            ),
            PCIVCostLine(
                model_id="azure-codegen",
                input_tokens=200,
                output_tokens=100,
                cost_usd=0.00075,
                role="implementer",
            ),
        ],
        total_cost_usd=0.0015,
        output_text="[stub pciv output]",
    )


# ---------------------------------------------------------------------------
# Per-instance / per-arm execution
# ---------------------------------------------------------------------------


_FAILURE_BUCKETS = (
    "budget-exceeded",
    "iterate-exhausted",
    "timeout",
    "infra-error",
    "reject",
)


def _classify_failure(error: str | None) -> str | None:
    """Map a strategy error string to one of the five failure buckets.

    The strategies prefix errors with stable tags
    (``budget_exceeded:``, ``post_call_budget_exceeded:``,
    ``adapter_error:``, ``timeout:``, ``pciv_rejected:``,
    ``iterate_exhausted:``). Anything we don't recognise lands in
    ``infra-error`` so the histogram never silently drops failures.
    """

    if not error:
        return None
    e = error.lower()
    if "budget_exceeded" in e:
        return "budget-exceeded"
    if "iterate" in e:
        return "iterate-exhausted"
    if "timeout" in e:
        return "timeout"
    if "reject" in e:
        return "reject"
    return "infra-error"


def _empty_repo_snapshot() -> RepoSnapshot:
    """Stub-mode: harness does not clone or scan a repo."""

    return RepoSnapshot(
        root=Path("."),
        file_count=10,
        total_bytes=10_000,
        has_tests=True,
        has_type_config=True,
        languages=["py"],
    )


def _result_to_record(
    instance_id: str,
    arm: str,
    result: StrategyResult,
    decision_strategy: str,
    decision_model: str,
) -> dict[str, Any]:
    tokens_in = sum(inv.tokens_in for inv in result.model_trace)
    tokens_out = sum(inv.tokens_out for inv in result.model_trace)
    return {
        "instance_id": instance_id,
        "arm": arm,
        "decision_strategy": decision_strategy,
        "decision_model": decision_model,
        "success": bool(result.success),
        "cost_usd": float(result.cost_usd),
        "tokens_in": int(tokens_in),
        "tokens_out": int(tokens_out),
        "latency_seconds": float(result.latency_seconds),
        "failure_mode": _classify_failure(result.error),
        "error": result.error,
    }


def run_instance_arm(
    *,
    instance_id: str,
    problem_statement: str,
    arm: str,
    policy_path: Path,
    budget_cap_usd: float,
    latency_target_seconds: int,
    adapter: Any,
    stub_pciv_runner: PCIVRunner,
) -> dict[str, Any]:
    """Run one arm for one instance through the existing Router.

    ``arm == "router"`` invokes the policy. The other three arms force
    the corresponding strategy via the same ``forced=`` channel the
    ``budgeteer run --force-strategy`` CLI uses.

    ``adapter`` is a ``StreamingChatClient``-compatible object: in stub
    mode an ``_StubAdapter`` instance, in live mode an
    ``AzureOpenAIAdapter`` from ``bench.live.runner._build_live_adapter``.
    """

    forced: str | None = None if arm == "router" else arm
    router = Router(
        policy_path=policy_path,
        budget_cap_usd=budget_cap_usd,
        adapter=adapter,  # type: ignore[arg-type]
        pciv_runner=stub_pciv_runner,
        auto_approve_pciv_gates=True,  # unattended harness run
    )
    snapshot = _empty_repo_snapshot()
    outcome = router.run(
        task=problem_statement,
        repo_snapshot=snapshot,
        latency_target_seconds=latency_target_seconds,
        forced=forced,
        task_id=f"{arm}/{instance_id}",
    )
    return _result_to_record(
        instance_id=instance_id,
        arm=arm,
        result=outcome.result,
        decision_strategy=outcome.decision.strategy,
        decision_model=outcome.decision.model,
    )


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _percentile(values: list[float], p: float) -> float:
    """Simple percentile. Empty list returns 0.0 (smoke-friendly)."""

    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def summarise_arm(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-arm summary: resolved-rate, cost, percentiles, failure modes."""

    n = len(records)
    if n == 0:
        return {
            "n": 0,
            "resolved": 0,
            "resolved_rate": 0.0,
            "total_cost_usd": 0.0,
            "cost_per_resolved_usd": None,
            "p50_latency_seconds": 0.0,
            "p95_latency_seconds": 0.0,
            "p50_tokens_in": 0,
            "p95_tokens_in": 0,
            "p50_tokens_out": 0,
            "p95_tokens_out": 0,
            "failure_modes": {b: 0 for b in _FAILURE_BUCKETS},
        }

    resolved = sum(1 for r in records if r["success"])
    total_cost = float(sum(r["cost_usd"] for r in records))
    cost_per_resolved = (total_cost / resolved) if resolved else None
    latencies = [float(r["latency_seconds"]) for r in records]
    tokens_in = [int(r["tokens_in"]) for r in records]
    tokens_out = [int(r["tokens_out"]) for r in records]
    failures = Counter(r["failure_mode"] for r in records if r["failure_mode"])
    failure_modes = {b: int(failures.get(b, 0)) for b in _FAILURE_BUCKETS}

    return {
        "n": n,
        "resolved": resolved,
        "resolved_rate": resolved / n,
        "total_cost_usd": round(total_cost, 6),
        "cost_per_resolved_usd": (
            round(cost_per_resolved, 6) if cost_per_resolved is not None else None
        ),
        "p50_latency_seconds": round(_percentile(latencies, 0.50), 6),
        "p95_latency_seconds": round(_percentile(latencies, 0.95), 6),
        "p50_tokens_in": int(round(_percentile([float(t) for t in tokens_in], 0.50))),
        "p95_tokens_in": int(round(_percentile([float(t) for t in tokens_in], 0.95))),
        "p50_tokens_out": int(round(_percentile([float(t) for t in tokens_out], 0.50))),
        "p95_tokens_out": int(round(_percentile([float(t) for t in tokens_out], 0.95))),
        "failure_modes": failure_modes,
    }


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(REPO_DIR),
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return "unknown"


def _config_hash(policy_path: Path) -> str:
    """SHA-256 of the policy file. Captured in the manifest for audit."""

    if not policy_path.is_file():
        return "missing"
    return hashlib.sha256(policy_path.read_bytes()).hexdigest()


def _path_for_manifest(path: Path) -> str:
    """Return ``path`` relative to the repo when it lives inside it, else absolute.

    The CLI's default ``--policy`` always sits under the repo so the
    manifest stays portable; tests (and any user-supplied override)
    may point at a temp path that ``relative_to`` would reject. Falling
    back to the absolute string keeps the manifest emittable in both
    cases without silently dropping the field.
    """

    try:
        return str(path.relative_to(REPO_DIR))
    except ValueError:
        return str(path)


def build_manifest(
    *,
    instances_file: Path,
    policy_path: Path,
    n_instances: int,
    arms: tuple[str, ...],
    started_at: str,
    finished_at: str,
    mode: str,
) -> dict[str, Any]:
    return {
        "run_uuid": str(uuid.uuid4()),
        "git_sha": _git_sha(),
        "config_hash": _config_hash(policy_path),
        "instances_file": _path_for_manifest(instances_file),
        "policy_file": _path_for_manifest(policy_path),
        "n_instances": int(n_instances),
        "arms": list(arms),
        "started_at": started_at,
        "finished_at": finished_at,
        "mode": mode,
        "schema_version": 1,
    }


# ---------------------------------------------------------------------------
# Problem-statement loader (handoff brief gap #1)
# ---------------------------------------------------------------------------


def load_problem_statements(
    path: Path, instance_ids: list[str]
) -> dict[str, str]:
    """Load a JSONL file of ``{"instance_id", "problem_statement"}`` records.

    Each line must parse to a JSON object with both fields. Returns a
    dict keyed by ``instance_id``. Raises ``ValueError`` if:

    - the file is missing,
    - any line is not valid JSON / is missing a required field,
    - any ``instance_id`` from ``instance_ids`` is absent from the file.

    Extra IDs in the file (not requested) are kept silently so the same
    JSONL can serve multiple instance lists. The fail-loud-on-missing
    check is the contract the v0.3 handoff brief asks for: a real run
    must never substitute a stub problem statement for a missing record.
    """

    if not path.is_file():
        raise FileNotFoundError(f"problem-statements file not found: {path}")

    problems: dict[str, str] = {}
    for lineno, raw in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{path}:{lineno}: not valid JSON ({exc.msg})"
            ) from exc
        if not isinstance(record, dict):
            raise ValueError(f"{path}:{lineno}: expected object, got {type(record).__name__}")
        instance_id = record.get("instance_id")
        problem = record.get("problem_statement")
        if not isinstance(instance_id, str) or not instance_id:
            raise ValueError(
                f"{path}:{lineno}: missing or non-string 'instance_id'"
            )
        if not isinstance(problem, str) or not problem:
            raise ValueError(
                f"{path}:{lineno}: missing or non-string 'problem_statement' "
                f"for {instance_id!r}"
            )
        problems[instance_id] = problem

    missing = sorted(set(instance_ids) - set(problems))
    if missing:
        raise ValueError(
            f"{path}: missing problem_statement for {len(missing)} requested "
            f"instance(s): {missing[:5]}"
            + (" ..." if len(missing) > 5 else "")
        )
    return problems


# ---------------------------------------------------------------------------
# Top-level run
# ---------------------------------------------------------------------------


def run(
    *,
    instances_file: Path,
    policy_path: Path,
    out_dir: Path,
    n_instances: int | None,
    budget_cap_usd: float,
    latency_target_seconds: int,
    arms: tuple[str, ...] = ARMS,
    problems_file: Path | None = None,
    live: bool = False,
) -> dict[str, Any]:
    """Harness driver. Stub mode is deterministic and ~$0.

    Live mode (``live=True``, gap #5):

    - Requires ``problems_file`` (no synthetic stubs in live mode).
    - Swaps the stub adapter for a real ``AzureOpenAIAdapter`` built
      via ``bench.live.runner._build_live_adapter("azure_openai")``,
      which validates ``AZURE_OPENAI_ENDPOINT`` + ``AZURE_OPENAI_API_KEY``
      and refuses to start without credentials.
    - Opens the cross-run ``PersistentBudgetLedger`` defined by
      ``[cross_run].cap_usd`` in ``policy.yaml`` (same wiring as the
      production CLI). Per-instance ``budget_cap_usd`` is capped by the
      remaining cross-run window so the four arms cannot collectively
      breach the daily/weekly cap.
    - PCIV runner is **still stubbed**: gap #3 (SWE-bench Docker eval
      pipeline) is punted to a follow-up. The ``router`` and ``pciv``
      arms in live mode therefore reflect routing-decision behaviour,
      not patch-passes-tests resolution. The manifest records this in
      its ``mode`` field and a ``limitations`` array.
    """

    if not instances_file.is_file():
        raise FileNotFoundError(f"instances file not found: {instances_file}")
    instance_ids = [
        line.strip()
        for line in instances_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if n_instances is not None:
        instance_ids = instance_ids[:n_instances]

    if live and problems_file is None:
        raise ValueError(
            "live mode requires --problems-file: refusing to send "
            "synthetic 'resolve {id}' prompts to a real provider."
        )

    problem_statements: dict[str, str] | None = None
    if problems_file is not None:
        problem_statements = load_problem_statements(problems_file, instance_ids)

    if live:
        # Imported here so stub-mode CI never pulls in env-var checks.
        from bench.live.runner import _build_live_adapter

        adapter: Any = _build_live_adapter("azure_openai")
    else:
        adapter = _StubAdapter()
    stub_pciv: PCIVRunner = _stub_pciv_runner

    # Cross-run ledger: in live mode, mirror the cli.py pattern so total
    # harness spend is gated by [cross_run].cap_usd. In stub mode this
    # block is skipped; stub spend is sub-cent and ledger churn would
    # add nothing.
    cross_run_ledger: Any = None
    effective_per_instance_cap = budget_cap_usd
    cross_run_window: str | None = None
    cross_run_cap_usd: float | None = None
    if live:
        from budgeteer.budget import load_cross_run as _load_cross_run

        cross_run_cfg = _load_cross_run(policy_path)
        if cross_run_cfg.cap_usd is not None:
            from agentcore.budget import PersistentBudgetLedger

            assert cross_run_cfg.db_path is not None  # invariant from load_cross_run
            cross_run_ledger = PersistentBudgetLedger(
                cross_run_cfg.db_path,
                cap_usd=cross_run_cfg.cap_usd,
                window=cross_run_cfg.window,
            )
            cross_run_window = cross_run_cfg.window
            cross_run_cap_usd = float(cross_run_cfg.cap_usd)
            remaining = cross_run_ledger.remaining_in_current_window()
            if remaining <= 0:
                # Capture spent BEFORE closing; the closed connection
                # cannot answer queries (sqlite3 ProgrammingError).
                spent = cross_run_ledger.spent_in_current_window()
                cross_run_ledger.close()
                raise RuntimeError(
                    f"cross-run {cross_run_cfg.window} cap exhausted: "
                    f"spent ${spent:.4f} "
                    f"/ cap ${cross_run_cfg.cap_usd:.4f}; refusing live run"
                )
            # Per-instance cap is the smaller of --budget and remaining.
            effective_per_instance_cap = min(budget_cap_usd, float(remaining))

    started_at = datetime.now(UTC).isoformat()
    arm_records: dict[str, list[dict[str, Any]]] = {a: [] for a in arms}

    try:
        for instance_id in instance_ids:
            if problem_statements is not None:
                problem_statement = problem_statements[instance_id]
            else:
                # Stub mode: synthetic problem statement keyed by instance_id.
                problem_statement = f"resolve {instance_id}"
            for arm in arms:
                record = run_instance_arm(
                    instance_id=instance_id,
                    problem_statement=problem_statement,
                    arm=arm,
                    policy_path=policy_path,
                    budget_cap_usd=effective_per_instance_cap,
                    latency_target_seconds=latency_target_seconds,
                    adapter=adapter,
                    stub_pciv_runner=stub_pciv,
                )
                arm_records[arm].append(record)
                if cross_run_ledger is not None:
                    # Record per-arm actual spend so a long run cannot
                    # silently overshoot the cross-run cap. We swallow
                    # ``BudgetExceeded`` here so downstream arms still
                    # record (the next call's preflight will catch it
                    # if remaining hits zero).
                    from agentcore.budget import (
                        BudgetExceeded as _BudgetExceeded,
                    )

                    try:
                        cross_run_ledger.record_spend(
                            float(record["cost_usd"]),
                            note=f"{arm}/{instance_id}"[:64],
                        )
                    except _BudgetExceeded:
                        pass
                    # Tighten the per-instance cap on the fly so the
                    # next Router build sees up-to-date headroom.
                    effective_per_instance_cap = min(
                        budget_cap_usd,
                        float(cross_run_ledger.remaining_in_current_window()),
                    )
    finally:
        if cross_run_ledger is not None:
            cross_run_ledger.close()

    finished_at = datetime.now(UTC).isoformat()

    manifest = build_manifest(
        instances_file=instances_file,
        policy_path=policy_path,
        n_instances=len(instance_ids),
        arms=arms,
        started_at=started_at,
        finished_at=finished_at,
        mode="live" if live else "stub",
    )
    if live:
        # Gaps explicitly punted in this milestone. Surfaced in the
        # manifest so a downstream REPORT.md cannot silently treat the
        # numbers as a finished benchmark.
        manifest["limitations"] = [
            "PCIV runner is the stub (gap #3: SWE-bench Docker eval not yet wired)",
            "RepoSnapshot is empty (gap #4: live repo scan not yet wired)",
            "resolved == strategy.success, NOT patch-passes-tests",
        ]
        if cross_run_cap_usd is not None:
            manifest["cross_run"] = {
                "window": cross_run_window,
                "cap_usd": cross_run_cap_usd,
            }

    payload: dict[str, Any] = {
        "manifest": manifest,
        "arms": {
            arm: {
                "instances": arm_records[arm],
                "summary": summarise_arm(arm_records[arm]),
            }
            for arm in arms
        },
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "results.json"
    out_path.write_text(
        json.dumps(payload, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Four-arm SWE-bench Verified harness (v0.3, stub-mode only). "
            "See docs/plans/v0.3-handoff-brief.md for the live-mode TODO list."
        )
    )
    parser.add_argument(
        "--instances",
        type=Path,
        default=DEFAULT_INSTANCES_FILE,
        help=f"Instance ID list file. Default: {DEFAULT_INSTANCES_FILE}",
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=DEFAULT_POLICY_PATH,
        help=f"Policy YAML. Default: {DEFAULT_POLICY_PATH}",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=REPO_DIR / "bench" / "results" / "smoke",
        help="Output directory. Default: bench/results/smoke",
    )
    parser.add_argument(
        "--n",
        type=int,
        default=5,
        help="Number of instances (default 5 for smoke; None to use all).",
    )
    parser.add_argument(
        "--budget",
        type=float,
        default=2.00,
        help="Per-instance, per-arm budget cap in USD. Default $2.00.",
    )
    parser.add_argument(
        "--latency",
        type=int,
        default=600,
        help="Latency target in seconds. Default 600.",
    )
    parser.add_argument(
        "--problems-file",
        type=Path,
        default=None,
        dest="problems_file",
        help=(
            "JSONL of {instance_id, problem_statement} records. When set, "
            "the harness uses real problem statements (handoff brief gap #1) "
            "instead of the synthetic stub. Required for any non-stub run."
        ),
    )
    parser.add_argument(
        "--live",
        action="store_true",
        default=False,
        help=(
            "Live mode (handoff brief gap #5). Swaps the stub adapter for "
            "a real AzureOpenAIAdapter built from AZURE_OPENAI_ENDPOINT / "
            "AZURE_OPENAI_API_KEY env vars. Requires --problems-file. "
            "Honours [cross_run].cap_usd in policy.yaml. PCIV runner is "
            "still stubbed (gap #3 punted); manifest surfaces this."
        ),
    )
    args = parser.parse_args(argv)

    payload = run(
        instances_file=args.instances,
        policy_path=args.policy,
        out_dir=args.out,
        n_instances=args.n,
        budget_cap_usd=args.budget,
        latency_target_seconds=args.latency,
        problems_file=args.problems_file,
        live=args.live,
    )

    out_path = args.out / "results.json"
    print(f"wrote {_path_for_manifest(out_path)}")
    for arm, block in payload["arms"].items():
        s = block["summary"]
        print(
            f"  {arm:<6} n={s['n']} resolved={s['resolved']}/{s['n']} "
            f"cost=${s['total_cost_usd']:.6f} "
            f"p50_lat={s['p50_latency_seconds']:.4f}s "
            f"failures={sum(s['failure_modes'].values())}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
