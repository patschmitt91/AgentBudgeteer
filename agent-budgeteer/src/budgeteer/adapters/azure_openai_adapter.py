"""Azure OpenAI adapter. Satisfies the StreamingChatClient protocol.

Wraps the official ``openai`` SDK's ``AzureOpenAI`` client in the same narrow
``get_response`` shape as the Anthropic adapter so strategies can swap
backends without changing their call sites.

This adapter is retained even though no strategy wires it in by default. It
is the intended backend for an Azure-hosted SingleAgent route and mirrors
the surface area agent-framework's Azure OpenAI chat client exposes. Tests
use a fake client that also satisfies the protocol.

Model identifiers in Azure OpenAI are deployment names, not base model
names, so the caller passes the deployment it wants to hit through ``model``.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from typing import Any

from .anthropic_adapter import AdapterMessage, AdapterResponse, OnTextCallback


class AzureOpenAIAdapter:
    """Thin harness around `openai.AzureOpenAI`.

    Authenticates with either an API key (``AZURE_OPENAI_API_KEY``) or an
    injected client. Uses the chat completions streaming API and aggregates
    token usage from the final stream chunk.
    """

    def __init__(
        self,
        *,
        azure_endpoint: str | None = None,
        api_key: str | None = None,
        api_version: str | None = None,
        client: Any | None = None,
        timeout_seconds: float = 60.0,
        max_retries: int = 2,
    ) -> None:
        if client is not None:
            self._client = client
            return
        endpoint = azure_endpoint or os.environ.get("AZURE_OPENAI_ENDPOINT")
        key = api_key or os.environ.get("AZURE_OPENAI_API_KEY")
        version = api_version or os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21")
        if not endpoint:
            raise RuntimeError(
                "AZURE_OPENAI_ENDPOINT not set and no explicit azure_endpoint or client passed"
            )
        if not key:
            raise RuntimeError(
                "AZURE_OPENAI_API_KEY not set and no explicit api_key or client passed"
            )
        from openai import AzureOpenAI

        self._client = AzureOpenAI(
            azure_endpoint=endpoint,
            api_key=key,
            api_version=version,
            timeout=timeout_seconds,
            max_retries=max_retries,
        )

    def get_response(
        self,
        messages: list[AdapterMessage],
        *,
        model: str,
        max_tokens: int,
        system: str | None = None,
        on_text: OnTextCallback | None = None,
    ) -> AdapterResponse:
        started = time.perf_counter()

        payload: list[dict[str, Any]] = []
        effective_system = system
        if effective_system is None:
            sys_msgs = [m.content for m in messages if m.role == "system"]
            if sys_msgs:
                effective_system = "\n\n".join(sys_msgs)
        if effective_system:
            payload.append({"role": "system", "content": effective_system})
        payload.extend(
            {"role": m.role, "content": m.content}
            for m in messages
            if m.role in ("user", "assistant")
        )

        stream = self._client.chat.completions.create(
            model=model,
            messages=payload,
            max_tokens=max_tokens,
            stream=True,
            stream_options={"include_usage": True},
        )

        final_text_parts: list[str] = []
        tokens_in = 0
        tokens_out = 0
        last_chunk: Any = None

        for chunk in _iter_chunks(stream):
            last_chunk = chunk
            choices = getattr(chunk, "choices", None) or []
            if choices:
                delta = getattr(choices[0], "delta", None)
                content = getattr(delta, "content", None) if delta is not None else None
                if content:
                    final_text_parts.append(content)
                    if on_text is not None:
                        on_text(content)
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                tokens_in = int(getattr(usage, "prompt_tokens", 0) or 0)
                tokens_out = int(getattr(usage, "completion_tokens", 0) or 0)

        latency_ms = int((time.perf_counter() - started) * 1000)
        return AdapterResponse(
            text="".join(final_text_parts),
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            raw=last_chunk,
        )


def _iter_chunks(stream: Any) -> Iterator[Any]:
    return iter(stream)
