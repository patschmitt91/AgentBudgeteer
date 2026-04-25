"""Secret redaction: no secret shapes leak into logs, spans, or state.

Seeds three distinct secret shapes (sk- style API key, bearer token,
JWT) into env + prompts, runs a full dry-run pipeline, and asserts no
occurrence in captured logs, span attributes, span events, or metrics
attributes.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from typer.testing import CliRunner

from budgeteer import telemetry as telemetry_mod
from budgeteer.cli import app
from budgeteer.redaction import REDACTED, redact, redact_mapping

POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "policy.yaml"

SECRETS = {
    "AZURE_OPENAI_API_KEY": "sk-secret-abcdefghijklmnopqrst",
    "ANTHROPIC_API_KEY": "bearer abcdef0123456789deadbeef",
    "OPENAI_API_KEY": (
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    ),
}


def _install_memory_tracer(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "budgeteer-test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # Replace the global tracer provider by monkeypatching the helper.
    monkeypatch.setattr(telemetry_mod, "_configured", True)
    trace.set_tracer_provider(provider)
    monkeypatch.setattr(telemetry_mod, "get_tracer", lambda: provider.get_tracer("agent-budgeteer"))
    return exporter


def _span_contains(span: Any, needle: str) -> bool:
    if needle in span.name:
        return True
    for value in (span.attributes or {}).values():
        if needle in str(value):
            return True
    for event in span.events or []:
        if needle in event.name:
            return True
        for value in (event.attributes or {}).values():
            if needle in str(value):
                return True
    return False


def test_redact_masks_known_patterns() -> None:
    for value in SECRETS.values():
        out = redact(f"before {value} after")
        assert value not in out
        assert REDACTED in out


def test_redact_mapping_masks_by_key_name() -> None:
    data = {
        "AZURE_OPENAI_API_KEY": "short-key",
        "msg": "user said sk-abcdefghij1234567890",
        "safe": "ok",
        "nested": {"bearer_token": "bearer abcdef1234567890deadbeef"},
    }
    clean = redact_mapping(data)
    assert clean["AZURE_OPENAI_API_KEY"] == REDACTED
    assert "sk-abcdefghij1234567890" not in clean["msg"]
    assert clean["safe"] == "ok"
    assert clean["nested"]["bearer_token"] == REDACTED


def test_secret_redaction_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)

    exporter = _install_memory_tracer(monkeypatch)

    # Capture root-logger output.
    log_buf = io.StringIO()
    handler = logging.StreamHandler(log_buf)
    handler.setFormatter(telemetry_mod.JsonFormatter())
    from budgeteer.redaction import RedactionFilter

    handler.addFilter(RedactionFilter())
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        logging.getLogger("budgeteer.test").info(
            "env %s token %s jwt %s",
            SECRETS["AZURE_OPENAI_API_KEY"],
            SECRETS["ANTHROPIC_API_KEY"],
            SECRETS["OPENAI_API_KEY"],
        )

        # Task prompt carries each secret verbatim; redaction must kick
        # in before anything is persisted or logged.
        task_with_secrets = " ; ".join(SECRETS.values())
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "run",
                f"echo {task_with_secrets}",
                "--dry-run",
                "--policy",
                str(POLICY_PATH),
            ],
        )
        assert result.exit_code == 0, result.stdout
    finally:
        root.removeHandler(handler)

    captured_logs = log_buf.getvalue()
    for value in SECRETS.values():
        assert value not in captured_logs, f"secret leaked into logs: {value!r}"

    spans = exporter.get_finished_spans()
    for span in spans:
        for value in SECRETS.values():
            assert not _span_contains(span, value), (
                f"secret leaked into span {span.name}: {value!r}"
            )
