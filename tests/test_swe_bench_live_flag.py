"""Tests for the SWE-bench harness ``--live`` flag (handoff brief gap #5).

Validates the live-mode wiring: real adapter swap, problems-file
requirement, manifest mode/limitations, and cross-run ledger gating.
Uses a fake Azure OpenAI client (no network, no key) injected through
the same ``_build_live_adapter`` chokepoint live runs use.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

REPO_DIR = Path(__file__).resolve().parents[1]
if str(REPO_DIR) not in sys.path:  # pragma: no cover - import side effect
    sys.path.insert(0, str(REPO_DIR))

from bench.live import runner as live_runner  # noqa: E402
from bench.swe_bench.runner import (  # noqa: E402
    DEFAULT_INSTANCES_FILE,
    DEFAULT_POLICY_PATH,
    run,
)

from budgeteer.adapters.azure_openai_adapter import AzureOpenAIAdapter  # noqa: E402

# ---------------------------------------------------------------------------
# Fake Azure client (mirrors tests/test_azure_openai_adapter.py)
# ---------------------------------------------------------------------------


@dataclass
class _Delta:
    content: str | None


@dataclass
class _Choice:
    delta: _Delta


@dataclass
class _Usage:
    prompt_tokens: int
    completion_tokens: int


@dataclass
class _Chunk:
    choices: list[_Choice]
    usage: _Usage | None = None


class _ReusableCompletions:
    """Fake ``chat.completions`` that returns a fresh chunk list on each call.

    SingleAgent / Fleet make multiple adapter calls per harness run; the
    fake must hand back a usable iterator each time, not a one-shot
    generator.
    """

    def __init__(self, text: str, tokens_in: int, tokens_out: int) -> None:
        self._text = text
        self._tokens_in = tokens_in
        self._tokens_out = tokens_out
        self.call_count = 0

    def create(self, **kwargs: Any) -> list[_Chunk]:
        self.call_count += 1
        midpoint = max(1, len(self._text) // 2)
        return [
            _Chunk(choices=[_Choice(delta=_Delta(content=self._text[:midpoint]))]),
            _Chunk(choices=[_Choice(delta=_Delta(content=self._text[midpoint:]))]),
            _Chunk(
                choices=[],
                usage=_Usage(
                    prompt_tokens=self._tokens_in,
                    completion_tokens=self._tokens_out,
                ),
            ),
        ]


class _FakeChat:
    def __init__(self, completions: _ReusableCompletions) -> None:
        self.completions = completions


class _FakeAzureClient:
    def __init__(self, completions: _ReusableCompletions) -> None:
        self.chat = _FakeChat(completions)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n",
        encoding="utf-8",
    )


def _patch_live_adapter(
    monkeypatch: pytest.MonkeyPatch,
    text: str = "stub-azure-output",
    tokens_in: int = 30,
    tokens_out: int = 10,
) -> _ReusableCompletions:
    """Make ``_build_live_adapter`` return an Azure adapter wrapping a fake."""

    completions = _ReusableCompletions(text, tokens_in, tokens_out)
    fake_client = _FakeAzureClient(completions)

    def fake_build(provider: str) -> Any:
        assert provider == "azure_openai"
        return AzureOpenAIAdapter(client=fake_client)

    monkeypatch.setattr(live_runner, "_build_live_adapter", fake_build)
    return completions


def _problems_for_first_n(tmp_path: Path, n: int) -> Path:
    """Build a JSONL of real problem statements for the first N v0.3 IDs."""

    instance_ids = (
        DEFAULT_INSTANCES_FILE.read_text(encoding="utf-8").splitlines()[:n]
    )
    problems_path = tmp_path / "problems.jsonl"
    _write_jsonl(
        problems_path,
        [
            {"instance_id": iid, "problem_statement": f"REAL: {iid}"}
            for iid in instance_ids
        ],
    )
    return problems_path


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


def test_live_requires_problems_file(tmp_path: Path) -> None:
    """``--live`` without ``--problems-file`` raises before any adapter runs."""

    with pytest.raises(ValueError, match="live mode requires --problems-file"):
        run(
            instances_file=DEFAULT_INSTANCES_FILE,
            policy_path=DEFAULT_POLICY_PATH,
            out_dir=tmp_path / "results",
            n_instances=1,
            budget_cap_usd=2.00,
            latency_target_seconds=600,
            live=True,
        )


# ---------------------------------------------------------------------------
# Live single-instance dry-run round trip
# ---------------------------------------------------------------------------


def test_live_single_instance_uses_real_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One instance, all 4 arms, real adapter chokepoint exercised."""

    completions = _patch_live_adapter(monkeypatch)
    problems_path = _problems_for_first_n(tmp_path, 1)

    payload = run(
        instances_file=DEFAULT_INSTANCES_FILE,
        policy_path=DEFAULT_POLICY_PATH,
        out_dir=tmp_path / "results",
        n_instances=1,
        budget_cap_usd=2.00,
        latency_target_seconds=600,
        problems_file=problems_path,
        live=True,
    )

    # Manifest reflects live mode and surfaces limitations.
    assert payload["manifest"]["mode"] == "live"
    assert "limitations" in payload["manifest"]
    assert any("PCIV" in lim for lim in payload["manifest"]["limitations"])

    # Adapter actually got called (proves the swap happened, not the stub).
    assert completions.call_count >= 1, (
        "live mode did not exercise the Azure adapter chokepoint"
    )

    # Each arm emitted one record.
    for arm in payload["arms"].values():
        assert len(arm["instances"]) == 1


def test_live_writes_results_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live mode writes the same results.json shape as stub mode."""

    _patch_live_adapter(monkeypatch)
    problems_path = _problems_for_first_n(tmp_path, 1)
    out_dir = tmp_path / "results"

    run(
        instances_file=DEFAULT_INSTANCES_FILE,
        policy_path=DEFAULT_POLICY_PATH,
        out_dir=out_dir,
        n_instances=1,
        budget_cap_usd=2.00,
        latency_target_seconds=600,
        problems_file=problems_path,
        live=True,
    )

    on_disk = json.loads((out_dir / "results.json").read_text(encoding="utf-8"))
    assert on_disk["manifest"]["mode"] == "live"
    assert on_disk["manifest"]["schema_version"] == 1


# ---------------------------------------------------------------------------
# Cross-run ledger gating
# ---------------------------------------------------------------------------


def test_live_cross_run_cap_aborts_when_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-exhausted cross-run window blocks the run before any arm fires."""

    _patch_live_adapter(monkeypatch)
    problems_path = _problems_for_first_n(tmp_path, 1)

    # Build a policy.yaml with a cross-run cap pointing at a temp ledger.
    policy_src = DEFAULT_POLICY_PATH.read_text(encoding="utf-8")
    ledger_path = tmp_path / "cross_run.db"
    cross_run_block = (
        f"\ncross_run:\n"
        f"  cap_usd: 0.10\n"
        f"  window: daily\n"
        f"  db_path: {ledger_path.as_posix()}\n"
    )
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(policy_src + cross_run_block, encoding="utf-8")

    # Pre-exhaust the ledger so the harness's preflight check trips.
    from agentcore.budget import PersistentBudgetLedger

    with PersistentBudgetLedger(ledger_path, cap_usd=0.10, window="daily") as led:
        led.force_record(0.20, reason="pre-exhaust for test")

    with pytest.raises(RuntimeError, match="cross-run .* cap exhausted"):
        run(
            instances_file=DEFAULT_INSTANCES_FILE,
            policy_path=policy_path,
            out_dir=tmp_path / "results",
            n_instances=1,
            budget_cap_usd=2.00,
            latency_target_seconds=600,
            problems_file=problems_path,
            live=True,
        )


def test_live_records_spend_to_cross_run_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-arm actual spend lands in the cross-run ledger."""

    _patch_live_adapter(monkeypatch)
    problems_path = _problems_for_first_n(tmp_path, 1)

    policy_src = DEFAULT_POLICY_PATH.read_text(encoding="utf-8")
    ledger_path = tmp_path / "cross_run.db"
    cross_run_block = (
        f"\ncross_run:\n"
        f"  cap_usd: 1.00\n"
        f"  window: daily\n"
        f"  db_path: {ledger_path.as_posix()}\n"
    )
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(policy_src + cross_run_block, encoding="utf-8")

    payload = run(
        instances_file=DEFAULT_INSTANCES_FILE,
        policy_path=policy_path,
        out_dir=tmp_path / "results",
        n_instances=1,
        budget_cap_usd=2.00,
        latency_target_seconds=600,
        problems_file=problems_path,
        live=True,
    )

    # Manifest surfaces the cross-run config.
    assert payload["manifest"].get("cross_run") == {
        "window": "daily",
        "cap_usd": 1.00,
    }

    # Ledger has at least one row now (one per arm).
    from agentcore.budget import PersistentBudgetLedger

    with PersistentBudgetLedger(ledger_path, cap_usd=1.00, window="daily") as led:
        spent = led.spent_in_current_window()
    assert spent > 0.0, "cross-run ledger did not record any live spend"


# ---------------------------------------------------------------------------
# Stub-mode regression (live=False stays the default and unchanged)
# ---------------------------------------------------------------------------


def test_stub_mode_default_still_synthetic(tmp_path: Path) -> None:
    """``live=False`` (default) does not touch the live adapter chokepoint."""

    payload = run(
        instances_file=DEFAULT_INSTANCES_FILE,
        policy_path=DEFAULT_POLICY_PATH,
        out_dir=tmp_path / "results",
        n_instances=1,
        budget_cap_usd=2.00,
        latency_target_seconds=600,
    )
    assert payload["manifest"]["mode"] == "stub"
    assert "limitations" not in payload["manifest"]
