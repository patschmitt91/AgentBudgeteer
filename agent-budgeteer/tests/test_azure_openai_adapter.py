"""Tests for the Azure OpenAI adapter using a fake streaming client."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from budgeteer.adapters.anthropic_adapter import AdapterMessage
from budgeteer.adapters.azure_openai_adapter import AzureOpenAIAdapter


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
        self.chunks = chunks
        self.captured: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> list[_Chunk]:
        self.captured = kwargs
        return self.chunks


class _FakeChat:
    def __init__(self, completions: _FakeCompletions) -> None:
        self.completions = completions


class _FakeClient:
    def __init__(self, chunks: list[_Chunk]) -> None:
        self.chat = _FakeChat(_FakeCompletions(chunks))


def _chunks() -> list[_Chunk]:
    return [
        _Chunk(choices=[_Choice(delta=_Delta(content="Hello"))]),
        _Chunk(choices=[_Choice(delta=_Delta(content=", world"))]),
        _Chunk(choices=[], usage=_Usage(prompt_tokens=17, completion_tokens=3)),
    ]


def test_azure_openai_adapter_streams_and_reports_usage() -> None:
    client = _FakeClient(_chunks())
    adapter = AzureOpenAIAdapter(client=client)

    seen: list[str] = []
    response = adapter.get_response(
        [
            AdapterMessage(role="system", content="be terse"),
            AdapterMessage(role="user", content="say hi"),
        ],
        model="gpt-4o-deployment",
        max_tokens=128,
        on_text=seen.append,
    )

    assert response.text == "Hello, world"
    assert response.model == "gpt-4o-deployment"
    assert response.tokens_in == 17
    assert response.tokens_out == 3
    assert seen == ["Hello", ", world"]
    # The system prompt was promoted into the messages payload.
    sent = client.chat.completions.captured["messages"]
    assert sent[0] == {"role": "system", "content": "be terse"}
    assert sent[1] == {"role": "user", "content": "say hi"}
    assert client.chat.completions.captured["stream"] is True
    assert client.chat.completions.captured["max_tokens"] == 128


def test_azure_openai_adapter_explicit_system_overrides_messages() -> None:
    client = _FakeClient(_chunks())
    adapter = AzureOpenAIAdapter(client=client)
    adapter.get_response(
        [
            AdapterMessage(role="system", content="ignored"),
            AdapterMessage(role="user", content="hi"),
        ],
        model="d",
        max_tokens=10,
        system="override-wins",
    )
    sent = client.chat.completions.captured["messages"]
    assert sent[0]["content"] == "override-wins"


def test_azure_openai_adapter_requires_endpoint_or_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="AZURE_OPENAI_ENDPOINT"):
        AzureOpenAIAdapter()
