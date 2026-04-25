"""OpenTelemetry tracing + metrics + structured logging.

The single public surface is small: :func:`configure` to install a tracer
provider, :func:`configure_logging` to install the root logger format
(JSON or text) plus a secret-redaction filter, :func:`get_tracer` for
span helpers, and the module-level counters declared under the
``Metrics`` section below.

Counters are created lazily via an idempotent accessor; the OpenTelemetry
API returns no-op counters when no meter provider is configured, so this
is safe to call in tests and production alike.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.metrics import Counter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

from budgeteer.redaction import RedactionFilter, redact, refresh_env_cache

_LOG = logging.getLogger(__name__)
_TRACER_NAME = "agent-budgeteer"
_METER_NAME = "agent-budgeteer"
_configured = False
_logging_configured = False


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
        except Exception as exc:
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


# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------


_STANDARD_LOGRECORD_FIELDS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
        "message",
        "asctime",
    }
)


class JsonFormatter(logging.Formatter):
    """Minimal JSON log formatter.

    Emits ``ts`` (ISO8601 with UTC offset), ``level``, ``logger``, ``msg``,
    plus ``run_id`` / ``trace_id`` / ``span_id`` when they are in the
    current OTel span context or attached to the record via ``extra=``.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }

        run_id = getattr(record, "run_id", None)
        if run_id is not None:
            payload["run_id"] = str(run_id)

        span = trace.get_current_span()
        ctx = span.get_span_context() if span is not None else None
        if ctx is not None and ctx.is_valid:
            payload["trace_id"] = format(ctx.trace_id, "032x")
            payload["span_id"] = format(ctx.span_id, "016x")

        # Carry through any non-standard attributes supplied via ``extra``.
        for key, value in record.__dict__.items():
            if key in _STANDARD_LOGRECORD_FIELDS or key.startswith("_"):
                continue
            if key in payload:
                continue
            try:
                json.dumps(value)
            except TypeError:
                value = str(value)
            payload[key] = value

        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))

        return json.dumps(payload, separators=(",", ":"), sort_keys=False)


def _resolve_log_format(explicit: str | None) -> str:
    if explicit:
        return explicit.lower()
    env = os.environ.get("LOG_FORMAT")
    if env:
        return env.strip().lower()
    return "text" if sys.stderr.isatty() else "json"


def configure_logging(
    level: int = logging.INFO,
    fmt: str | None = None,
    *,
    force: bool = False,
) -> None:
    """Install a stderr handler with either a JSON or text formatter.

    ``LOG_FORMAT=json`` / ``LOG_FORMAT=text`` overrides auto-detection; by
    default a TTY gets text, anything else gets JSON. Always attaches
    :class:`~budgeteer.redaction.RedactionFilter` so secret-shaped
    substrings are scrubbed before a record is emitted.
    """

    # Snapshot env-secrets before the first record is emitted.
    refresh_env_cache()

    global _logging_configured
    root = logging.getLogger()
    if _logging_configured and not force:
        root.setLevel(level)
        return

    for h in list(root.handlers):
        if getattr(h, "_budgeteer_handler", False):
            root.removeHandler(h)

    handler = logging.StreamHandler(stream=sys.stderr)
    chosen = _resolve_log_format(fmt)
    if chosen == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
    handler.addFilter(RedactionFilter())
    handler._budgeteer_handler = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level)
    _logging_configured = True


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
#
# Counter and histogram caches live in :mod:`agentcore.telemetry`; the
# accessors below are thin name-bound wrappers so callers keep the same
# import surface.

from agentcore import telemetry as _core_telemetry  # noqa: E402


def runs_total() -> Counter:
    return _core_telemetry.get_counter(_METER_NAME, "runs_total", description="Total runs started.")


def runs_failed_total() -> Counter:
    return _core_telemetry.get_counter(
        _METER_NAME, "runs_failed_total", description="Runs that ended in failure."
    )


def budget_usd_spent_total() -> Counter:
    return _core_telemetry.get_counter(
        _METER_NAME,
        "budget_usd_spent_total",
        unit="USD",
        description="Cumulative USD spent across runs.",
    )


def routing_decisions_total() -> Counter:
    return _core_telemetry.get_counter(
        _METER_NAME,
        "routing_decisions_total",
        description="Routing decisions by strategy.",
    )


def cost_usd_per_run() -> metrics.Histogram:
    return _core_telemetry.get_histogram(
        _METER_NAME,
        "cost_usd_per_run",
        unit="USD",
        description="USD spent on a single run, recorded once per terminal status.",
    )


def latency_seconds_per_run() -> metrics.Histogram:
    return _core_telemetry.get_histogram(
        _METER_NAME,
        "latency_seconds_per_run",
        unit="s",
        description="Wall-clock seconds from CLI run start to terminal status.",
    )


def tokens_per_run() -> metrics.Histogram:
    return _core_telemetry.get_histogram(
        _METER_NAME,
        "tokens_per_run",
        description="Total tokens (input+output) charged across a single run.",
    )


def reset_counters_for_tests() -> None:
    """Clear the shared instrument cache (counters and histograms).

    Tests that swap the global ``MeterProvider`` via
    :func:`opentelemetry.metrics.set_meter_provider` must call this so
    instruments are re-created against the new provider.
    """

    _core_telemetry.reset_for_tests()


def set_meter_provider_for_tests(provider: metrics.MeterProvider | None) -> None:
    """Override the meter provider used by the module-level instruments.

    OpenTelemetry's global ``set_meter_provider`` is set-once, so tests
    use this hook instead to inject an in-memory ``MeterProvider``.
    Delegates to :func:`agentcore.telemetry.set_meter_provider_for_tests`.
    """

    _core_telemetry.set_meter_provider_for_tests(provider)
