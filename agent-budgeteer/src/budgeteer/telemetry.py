"""OpenTelemetry setup. Exports to Azure App Insights when configured."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

_LOG = logging.getLogger(__name__)
_TRACER_NAME = "agent-budgeteer"
_configured = False


def configure(service_name: str = "agent-budgeteer") -> None:
    """Idempotent tracer setup.

    Behaviour, in order:
      1. If ``APPLICATIONINSIGHTS_CONNECTION_STRING`` is set, export to Azure
         Monitor.
      2. Else if ``BUDGETEER_CONSOLE_TRACES`` is truthy, export spans to
         stdout (developer opt-in only; this corrupts JSON CLI output).
      3. Otherwise install a silent ``TracerProvider`` so spans are created
         but dropped. This is the default because the CLI emits JSON on
         stdout and a console exporter would interleave span blobs with it.
    """

    global _configured
    if _configured:
        return

    resource = Resource.create({"service.name": service_name})

    conn_str = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if conn_str:
        try:
            from azure.monitor.opentelemetry import configure_azure_monitor

            configure_azure_monitor(
                connection_string=conn_str,
                resource=resource,
                disable_logging=False,
                disable_metrics=False,
            )
            _configured = True
            return
        except Exception as exc:  # pragma: no cover - best effort
            _LOG.warning("azure monitor setup failed, falling back to silent: %s", exc)

    provider = TracerProvider(resource=resource)
    if _env_flag("BUDGETEER_CONSOLE_TRACES"):
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
    _configured = True


def _env_flag(name: str) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def get_tracer() -> trace.Tracer:
    if not _configured:
        configure()
    return trace.get_tracer(_TRACER_NAME)


@contextmanager
def strategy_span(strategy: str, task_id: str, **attributes: Any) -> Iterator[trace.Span]:
    """Convenience context manager around a strategy execution."""

    tracer = get_tracer()
    with tracer.start_as_current_span(f"strategy.{strategy}") as span:
        span.set_attribute("strategy", strategy)
        span.set_attribute("task_id", task_id)
        for k, v in attributes.items():
            span.set_attribute(k, v)
        yield span
