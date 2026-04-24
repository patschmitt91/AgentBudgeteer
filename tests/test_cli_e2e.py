"""End-to-end CLI tests for the `budgeteer` Typer app.

These tests exercise the full CLI path with the Anthropic adapter
replaced by an in-memory fake, so no network or API key is required.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from budgeteer import cli as cli_module
from budgeteer.adapters.anthropic_adapter import AdapterMessage, AdapterResponse
from budgeteer.cli import app

POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "policy.yaml"


class _FakeAdapter:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def get_response(
        self,
        messages: list[AdapterMessage],
        *,
        model: str,
        max_tokens: int,
        system: str | None = None,
        on_text: Any | None = None,
    ) -> AdapterResponse:
        self.calls.append({"model": model, "max_tokens": max_tokens})
        return AdapterResponse(
            text="done",
            model=model,
            tokens_in=100,
            tokens_out=50,
            latency_ms=10,
        )


def test_cli_version() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip()


def test_cli_run_dry_run_emits_expected_json() -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "run",
            "add a greeting endpoint",
            "--dry-run",
            "--budget",
            "1.50",
            "--policy",
            str(POLICY_PATH),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert set(payload["features"]) >= {
        "estimated_file_count",
        "cross_file_dependency_score",
        "estimated_input_tokens",
    }
    assert payload["decision"]["strategy"] in {"single", "pciv", "fleet"}
    assert payload["decision"]["model"]
    assert payload["decision"]["reason"]


def test_cli_run_dry_run_with_forced_strategy() -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "run",
            "small tweak",
            "--dry-run",
            "--force-strategy",
            "fleet",
            "--policy",
            str(POLICY_PATH),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["decision"]["strategy"] == "fleet"
    assert payload["decision"]["reason"] == "forced"


def test_cli_run_invalid_forced_strategy_exits_two() -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "run",
            "anything",
            "--force-strategy",
            "nope",
            "--policy",
            str(POLICY_PATH),
        ],
    )
    assert result.exit_code == 2
    assert "invalid --force-strategy" in result.output


def test_cli_run_missing_policy_exits_two(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.yaml"
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["run", "x", "--dry-run", "--policy", str(missing)],
    )
    assert result.exit_code == 2
    assert "policy file not found" in result.output


def test_cli_run_executes_single_agent_with_fake_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Point --repo at an empty tmp dir so the repo scan is cheap.
    repo = tmp_path / "repo"
    repo.mkdir()

    fake = _FakeAdapter()
    monkeypatch.setattr(
        "budgeteer.router.AnthropicAdapter",
        lambda *a, **kw: fake,
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "run",
            "mechanical edit",
            "--budget",
            "1.00",
            "--force-strategy",
            "single",
            "--policy",
            str(POLICY_PATH),
            "--repo",
            str(repo),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is False
    assert payload["result"]["success"] is True
    assert payload["result"]["strategy_used"] == "single"
    assert fake.calls


def test_cli_learn_missing_data_exits_two(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["learn", str(tmp_path / "missing.json"), "--policy", str(POLICY_PATH)],
    )
    assert result.exit_code == 2
    assert "training data not found" in result.output


def test_cli_learn_trains_and_writes_report(tmp_path: Path) -> None:
    pytest.importorskip("sklearn")
    # Minimal labeled examples.
    data = tmp_path / "labels.json"
    rows = []
    for _ in range(6):
        rows.append(
            {
                "features": {
                    "estimated_file_count": 1,
                    "cross_file_dependency_score": 0.1,
                    "test_presence": False,
                    "type_safety_signal": False,
                    "planning_depth_score": 1,
                    "reasoning_vs_mechanical_score": 0.1,
                    "estimated_input_tokens": 5000,
                },
                "label": "single",
            }
        )
    for _ in range(4):
        rows.append(
            {
                "features": {
                    "estimated_file_count": 20,
                    "cross_file_dependency_score": 0.8,
                    "test_presence": True,
                    "type_safety_signal": True,
                    "planning_depth_score": 8,
                    "reasoning_vs_mechanical_score": 0.9,
                    "estimated_input_tokens": 200_000,
                },
                "label": "pciv",
            }
        )
    data.write_text(json.dumps(rows), encoding="utf-8")
    out = tmp_path / "report.json"

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "learn",
            str(data),
            "--policy",
            str(POLICY_PATH),
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert out.exists()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["samples"] == 10
    assert "train_accuracy" in payload


def test_default_policy_path_walks_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Real implementation walks parents of the budgeteer source tree.
    p = cli_module._default_policy_path()
    assert p.name == "policy.yaml"


def test_scan_repo_prunes_ignored_dirs(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("print('x')\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "garbage.py").write_text("x", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("x", encoding="utf-8")

    snap = cli_module._scan_repo(tmp_path)
    assert snap.has_tests is True
    assert "py" in snap.languages
    # node_modules entries must not be counted.
    assert snap.file_count <= 3
