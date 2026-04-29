"""Stub-mode smoke test for the v0.3 four-arm SWE-bench harness.

This test runs the harness against 5 instances from the locked v0.3 list,
fully stubbed (no model calls, no SWE-bench Docker), and asserts the
emitted ``results.json`` is well-formed for all four arms.

It does NOT measure quality, cost, or latency \u2014 those are the
responsibility of a real run executed by an Agent Framework engineer
following ``docs/plans/v0.3-handoff-brief.md``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]
if str(REPO_DIR) not in sys.path:  # pragma: no cover - import side effect
    sys.path.insert(0, str(REPO_DIR))

from bench.swe_bench.runner import (  # noqa: E402
    ARMS,
    DEFAULT_INSTANCES_FILE,
    DEFAULT_POLICY_PATH,
    run,
)


def test_smoke_emits_well_formed_results(tmp_path: Path) -> None:
    out_dir = tmp_path / "results"
    payload = run(
        instances_file=DEFAULT_INSTANCES_FILE,
        policy_path=DEFAULT_POLICY_PATH,
        out_dir=out_dir,
        n_instances=5,
        budget_cap_usd=2.00,
        latency_target_seconds=600,
    )

    # Manifest shape.
    manifest = payload["manifest"]
    assert manifest["schema_version"] == 1
    assert manifest["n_instances"] == 5
    assert manifest["mode"] == "stub"
    assert manifest["arms"] == list(ARMS)
    assert manifest["run_uuid"]
    assert manifest["config_hash"] not in {"", "missing"}
    # git_sha is "unknown" in detached / non-git contexts; that's fine.

    # All four arms present.
    assert set(payload["arms"]) == set(ARMS)

    # Each arm has 5 records and a summary.
    for arm in ARMS:
        block = payload["arms"][arm]
        assert len(block["instances"]) == 5, f"{arm}: expected 5 instances"
        for record in block["instances"]:
            assert {
                "instance_id",
                "arm",
                "decision_strategy",
                "decision_model",
                "success",
                "cost_usd",
                "tokens_in",
                "tokens_out",
                "latency_seconds",
                "failure_mode",
                "error",
            } <= set(record), f"{arm}/{record.get('instance_id')}: missing fields"

        summary = block["summary"]
        assert summary["n"] == 5
        assert 0.0 <= summary["resolved_rate"] <= 1.0
        # Stubs charge a tiny but non-zero cost (azure-codegen pricing on
        # 100 in / 50 out plus PCIV stub's 0.0015). Sanity-check with a
        # generous ceiling so this test stays robust to projection
        # coefficient tweaks.
        assert summary["total_cost_usd"] < 0.10, summary
        assert isinstance(summary["failure_modes"], dict)
        for bucket in (
            "budget-exceeded",
            "iterate-exhausted",
            "timeout",
            "infra-error",
            "reject",
        ):
            assert bucket in summary["failure_modes"]

    # Round-trip: written file matches the in-memory payload.
    on_disk = json.loads((out_dir / "results.json").read_text(encoding="utf-8"))
    assert on_disk["manifest"]["run_uuid"] == manifest["run_uuid"]
    assert set(on_disk["arms"]) == set(ARMS)


def test_forced_arms_select_their_strategy(tmp_path: Path) -> None:
    """Each forced arm's decision_strategy must equal the arm name."""

    payload = run(
        instances_file=DEFAULT_INSTANCES_FILE,
        policy_path=DEFAULT_POLICY_PATH,
        out_dir=tmp_path,
        n_instances=2,
        budget_cap_usd=2.00,
        latency_target_seconds=600,
    )
    for arm in ("single", "pciv", "fleet"):
        for record in payload["arms"][arm]["instances"]:
            assert record["decision_strategy"] == arm, (
                f"forced arm {arm} produced decision_strategy={record['decision_strategy']!r}"
            )

    # Router arm: classifier picks; just assert it's one of the three valid
    # strategies, not necessarily a specific one.
    for record in payload["arms"]["router"]["instances"]:
        assert record["decision_strategy"] in {"single", "pciv", "fleet"}
