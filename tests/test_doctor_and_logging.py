"""Tests for `budgeteer doctor` and `--verbose` / `--quiet` / `LOG_FORMAT`."""

from __future__ import annotations

import json
import logging

import pytest
from typer.testing import CliRunner

from budgeteer import telemetry as telemetry_mod
from budgeteer.cli import app
from budgeteer.redaction import REDACTED


def test_doctor_reports_core_checks_and_exits_zero_when_green() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])
    # In a dev checkout Python+git+config should all be present.
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    labels = {c["check"] for c in payload["checks"]}
    assert {"python", "git", "config", "env", "os"}.issubset(labels)


def test_doctor_redacts_env_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-visible-should-not-appear")
    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "sk-visible-should-not-appear" not in result.output
    assert REDACTED in result.output


def test_verbose_and_quiet_are_mutually_exclusive() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["--verbose", "--quiet", "doctor"])
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_verbose_sets_debug_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(telemetry_mod, "_logging_configured", False)
    runner = CliRunner()
    result = runner.invoke(app, ["--verbose", "doctor"])
    assert result.exit_code == 0, result.output
    assert logging.getLogger().level == logging.DEBUG


def test_quiet_sets_warning_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(telemetry_mod, "_logging_configured", False)
    runner = CliRunner()
    result = runner.invoke(app, ["--quiet", "doctor"])
    assert result.exit_code == 0, result.output
    assert logging.getLogger().level == logging.WARNING


def test_log_format_env_forces_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.setattr(telemetry_mod, "_logging_configured", False)
    telemetry_mod.configure_logging(level=logging.INFO, force=True)
    root = logging.getLogger()
    json_fmt = any(isinstance(h.formatter, telemetry_mod.JsonFormatter) for h in root.handlers)
    assert json_fmt
