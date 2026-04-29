"""Tests for the Azure OpenAI live-bench cassette path.

Exercises ``bench.live.runner.run_live`` and ``run_replay`` with the
``provider="azure_openai"`` task while injecting a fake Azure OpenAI
client (no network, no key). The fake mirrors the chunk shape used by
``tests/test_azure_openai_adapter.py`` so the adapter under test is
exactly what live runs use.

Three contracts asserted:

1. ``run_live`` for an Azure task records a well-formed cassette with
   ``provider == "azure_openai"`` and the chunked text aggregated.
2. ``run_replay`` against that cassette returns ``success=True`` and
   ``actual_cost_usd == cassette.totals.cost_usd``.
3. Cassette redaction scrubs Azure-shaped secrets (``api-key`` headers
   bleeding into a response, full Azure endpoint URLs with key params).
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
from bench.live.cassette import (  # noqa: E402
    Cassette,
    CassetteCall,
    new_cassette,
)
from bench.live.runner import (  # noqa: E402
    LiveBenchTask,
    run_live,
    run_replay,
)

from budgeteer.adapters.azure_openai_adapter import AzureOpenAIAdapter  # noqa: E402

# ---------------------------------------------------------------------------
# Fake Azure OpenAI client (mirrors tests/test_azure_openai_adapter.py)
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


class _FakeCompletions:
    def __init__(self, chunks: list[_Chunk]) -> None:
        self._chunks = chunks
        self.captured: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> list[_Chunk]:
        self.captured = kwargs
        return self._chunks


class _FakeChat:
    def __init__(self, completions: _FakeCompletions) -> None:
        self.completions = completions


class _FakeAzureClient:
    def __init__(self, chunks: list[_Chunk]) -> None:
        self.chat = _FakeChat(_FakeCompletions(chunks))


def _stream_chunks(text: str, tokens_in: int, tokens_out: int) -> list[_Chunk]:
    """Build a chunk stream that aggregates to ``text`` with the given usage.

    Splits ``text`` into halves so the on_text callback fires twice; the
    final chunk carries the usage payload (matches the real Azure SDK
    when ``stream_options.include_usage=True``).
    """

    midpoint = max(1, len(text) // 2)
    return [
        _Chunk(choices=[_Choice(delta=_Delta(content=text[:midpoint]))]),
        _Chunk(choices=[_Choice(delta=_Delta(content=text[midpoint:]))]),
        _Chunk(choices=[], usage=_Usage(prompt_tokens=tokens_in, completion_tokens=tokens_out)),
    ]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def azure_task() -> LiveBenchTask:
    """Load the canonical Azure micro-bench task fixture."""

    task_path = (
        REPO_DIR / "bench" / "live" / "tasks" / "task_02_reverse_string_azure.yaml"
    )
    return LiveBenchTask.load(task_path)


@pytest.fixture
def isolated_ledger_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Redirect the per-task ledger DB into ``tmp_path``.

    Without this, every recorded test run would share a daily window
    with the on-disk ``bench/live/.ledger/`` and could either pollute
    real recordings or trip the cap on repeat runs.
    """

    ledger = tmp_path / "ledger"
    monkeypatch.setattr(live_runner, "LEDGER_DIR", ledger)
    return ledger


# ---------------------------------------------------------------------------
# Record + replay round trip
# ---------------------------------------------------------------------------


def test_run_live_records_azure_cassette(
    azure_task: LiveBenchTask,
    tmp_path: Path,
    isolated_ledger_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live run with an injected fake client emits a valid cassette."""

    chunks = _stream_chunks(
        "def reverse_string(s):\n    return s[::-1]\n",
        tokens_in=42,
        tokens_out=18,
    )
    fake_client = _FakeAzureClient(chunks)

    def fake_build(provider: str) -> Any:
        assert provider == "azure_openai"
        return AzureOpenAIAdapter(client=fake_client)

    monkeypatch.setattr(live_runner, "_build_live_adapter", fake_build)

    cassette_path = tmp_path / f"{azure_task.id}.json"
    report = run_live(azure_task, cassette_path=cassette_path)

    assert report.success, f"live run failed: notes={report.notes!r}"
    assert report.cost_under_cap
    assert report.actual_cost_usd > 0.0, "fake client should produce non-zero cost"

    # Cassette was written and is well-formed.
    assert cassette_path.is_file()
    cassette = Cassette.load(cassette_path)
    assert cassette.provider == "azure_openai"
    assert cassette.model == azure_task.model
    assert cassette.task_id == azure_task.id
    assert len(cassette.calls) >= 1
    # SingleAgent makes one adapter call; the recorded text is what the
    # fake produced.
    first = cassette.calls[0]
    assert first.response["text"] == "def reverse_string(s):\n    return s[::-1]\n"
    assert first.response["tokens_in"] == 42
    assert first.response["tokens_out"] == 18


def test_run_replay_round_trips_azure_cassette(
    azure_task: LiveBenchTask,
    tmp_path: Path,
    isolated_ledger_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cassette recorded with the fake replays cleanly with no client."""

    chunks = _stream_chunks(
        "def reverse_string(s):\n    return s[::-1]\n",
        tokens_in=42,
        tokens_out=18,
    )
    fake_client = _FakeAzureClient(chunks)
    monkeypatch.setattr(
        live_runner,
        "_build_live_adapter",
        lambda _provider: AzureOpenAIAdapter(client=fake_client),
    )

    cassette_path = tmp_path / f"{azure_task.id}.json"
    record_report = run_live(azure_task, cassette_path=cassette_path)
    assert record_report.success

    replay_report = run_replay(azure_task, cassette_path=cassette_path)
    assert replay_report.success, f"replay failed: notes={replay_report.notes!r}"
    assert replay_report.actual_strategy == azure_task.expected_strategy
    # Replay cost equals recorded total within the runner's tolerance.
    cassette = Cassette.load(cassette_path)
    assert abs(replay_report.actual_cost_usd - cassette.totals.cost_usd) <= 1e-9


# ---------------------------------------------------------------------------
# Provider validation
# ---------------------------------------------------------------------------


def test_build_live_adapter_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError, match="unsupported provider"):
        live_runner._build_live_adapter("openai")


def test_build_live_adapter_azure_requires_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="AZURE_OPENAI_ENDPOINT"):
        live_runner._build_live_adapter("azure_openai")


def test_build_live_adapter_anthropic_requires_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        live_runner._build_live_adapter("anthropic")


# ---------------------------------------------------------------------------
# Azure-shaped redaction
# ---------------------------------------------------------------------------


def test_save_redacts_azure_endpoint_url_with_key(tmp_path: Path) -> None:
    """An Azure deployment URL with an embedded api-key must be scrubbed.

    Real Azure OpenAI keys are 32+ char hex strings, which the central
    ``hex_blob`` redaction regex catches. The text URL part is not a
    secret and survives redaction; only the key digits are scrubbed.
    """

    leaky = (
        "POST https://contoso.openai.azure.com/openai/deployments/gpt-4o/"
        "chat/completions?api-version=2024-10-21 with api-key: "
        "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
    )
    cassette = new_cassette(
        task_id="t", provider="azure_openai", model="azure-codegen"
    )
    cassette.calls.append(
        CassetteCall(
            request={
                "messages": [{"role": "user", "content": "hi"}],
                "model": "azure-codegen",
                "max_tokens": 16,
            },
            response={
                "text": leaky,
                "model": "azure-codegen",
                "tokens_in": 5,
                "tokens_out": 12,
                "latency_ms": 7,
            },
        )
    )
    out = tmp_path / "cassette.json"
    cassette.save(out)

    text = out.read_text(encoding="utf-8")
    assert (
        "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
        not in text
    )
    # Non-secret URL parts survive redaction; only the secret is scrubbed.
    assert "contoso.openai.azure.com" in text


def test_save_redacts_azure_api_key_env_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A literal value held in ``AZURE_OPENAI_API_KEY`` must be scrubbed."""

    secret = "azure-key-value-NotInRegexCatalogue-2468ace"
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", secret)
    from agentcore.redaction import refresh_env_cache

    refresh_env_cache()

    cassette = new_cassette(
        task_id="t", provider="azure_openai", model="azure-codegen"
    )
    cassette.calls.append(
        CassetteCall(
            request={
                "messages": [{"role": "user", "content": f"the key is {secret}"}],
                "model": "azure-codegen",
                "max_tokens": 8,
            },
            response={
                "text": "ok",
                "model": "azure-codegen",
                "tokens_in": 4,
                "tokens_out": 1,
                "latency_ms": 3,
            },
        )
    )
    out = tmp_path / "cassette.json"
    cassette.save(out)

    text = out.read_text(encoding="utf-8")
    assert secret not in text


# ---------------------------------------------------------------------------
# Schema portability
# ---------------------------------------------------------------------------


def test_azure_cassette_schema_matches_anthropic(tmp_path: Path) -> None:
    """The cassette schema is provider-agnostic apart from ``provider``."""

    azure_cassette = new_cassette(
        task_id="t", provider="azure_openai", model="azure-codegen"
    )
    anthropic_cassette = new_cassette(
        task_id="t", provider="anthropic", model="anthropic-fallback"
    )

    az_path = tmp_path / "azure.json"
    an_path = tmp_path / "anthropic.json"
    azure_cassette.save(az_path)
    anthropic_cassette.save(an_path)

    az = json.loads(az_path.read_text(encoding="utf-8"))
    an = json.loads(an_path.read_text(encoding="utf-8"))

    # Same top-level keys, same schema_version.
    assert set(az) == set(an)
    assert az["schema_version"] == an["schema_version"]
    assert az["provider"] == "azure_openai"
    assert an["provider"] == "anthropic"
