"""Counters are emitted with the expected names."""

from __future__ import annotations

from pathlib import Path

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from typer.testing import CliRunner

from budgeteer import telemetry as telemetry_mod
from budgeteer.adapters.anthropic_adapter import AdapterMessage, AdapterResponse
from budgeteer.cli import app

POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "policy.yaml"


class _FakeAdapter:
    def get_response(
        self,
        messages: list[AdapterMessage],
        *,
        model: str,
        max_tokens: int,
        system: str | None = None,
        on_text: object | None = None,
    ) -> AdapterResponse:
        return AdapterResponse(text="ok", model=model, tokens_in=10, tokens_out=5, latency_ms=1)


@pytest.fixture
def metric_reader(monkeypatch: pytest.MonkeyPatch) -> InMemoryMetricReader:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    telemetry_mod.set_meter_provider_for_tests(provider)
    yield reader
    telemetry_mod.set_meter_provider_for_tests(None)


def _counter_names(reader: InMemoryMetricReader) -> set[str]:
    data = reader.get_metrics_data()
    names: set[str] = set()
    if data is None:
        return names
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                names.add(m.name)
    return names


def test_routing_decisions_counter_emitted_on_dry_run(
    metric_reader: InMemoryMetricReader,
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["run", "small edit", "--dry-run", "--policy", str(POLICY_PATH)],
    )
    assert result.exit_code == 0, result.stdout
    names = _counter_names(metric_reader)
    assert "routing_decisions_total" in names


def test_runs_and_budget_counters_emitted_on_execution(
    metric_reader: InMemoryMetricReader,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("budgeteer.router.AnthropicAdapter", lambda *a, **kw: _FakeAdapter())
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "run",
            "mechanical edit",
            "--force-strategy",
            "single",
            "--budget",
            "1.00",
            "--policy",
            str(POLICY_PATH),
            "--repo",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.stdout
    names = _counter_names(metric_reader)
    assert {"runs_total", "budget_usd_spent_total", "routing_decisions_total"}.issubset(names)
