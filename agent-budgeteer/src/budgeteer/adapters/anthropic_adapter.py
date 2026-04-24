"""Anthropic adapter. Wraps the official SDK in an agent-framework-shaped harness.

agent-framework does not ship a native Anthropic chat client at the pinned
version. This adapter exposes a narrow `get_response` method that mirrors the
shape of `agent_framework` chat clients so strategies can swap backends
without changing their call sites.

The adapter supports synchronous streaming through the Anthropic SDK and
reports token usage back to the caller.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class AdapterMessage:
    role: str  # "system", "user", "assistant"
    content: str


@dataclass
class AdapterResponse:
    text: str
    model: str
    tokens_in: int
    tokens_out: int
    latency_ms: int
    raw: Any = field(default=None)


class StreamingChatClient(Protocol):
    """Structural type every adapter must satisfy."""

    def get_response(
        self,
        messages: list[AdapterMessage],
        *,
        model: str,
        max_tokens: int,
        system: str | None = None,
        on_text: OnTextCallback | None = None,
    ) -> AdapterResponse: ...


class OnTextCallback(Protocol):
    def __call__(self, delta: str) -> None: ...


class AnthropicAdapter:
    """Thin harness around `anthropic.Anthropic`.

    Streams text via `client.messages.stream` and returns an
    `AdapterResponse` with usage counters. The caller passes `on_text` to
    observe incremental tokens without coupling to the Anthropic SDK types.
    """

    def __init__(
        self,
        api_key: str | None = None,
        client: Any | None = None,
        *,
        timeout_seconds: float = 60.0,
        max_retries: int = 2,
    ) -> None:
        if client is not None:
            self._client = client
            return
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY not set and no explicit api_key or client passed")
        # Imported lazily so tests that inject a fake client do not need the SDK.
        from anthropic import Anthropic

        self._client = Anthropic(
            api_key=key,
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
        payload: list[dict[str, Any]] = [
            {"role": m.role, "content": m.content}
            for m in messages
            if m.role in ("user", "assistant")
        ]
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": payload,
        }
        effective_system = system
        if effective_system is None:
            sys_msgs = [m.content for m in messages if m.role == "system"]
            if sys_msgs:
                effective_system = "\n\n".join(sys_msgs)
        if effective_system:
            kwargs["system"] = effective_system

        final_text_parts: list[str] = []
        tokens_in = 0
        tokens_out = 0
        raw_final: Any = None

        stream_cm = self._client.messages.stream(**kwargs)
        with stream_cm as stream:
            for delta in _iter_text(stream):
                final_text_parts.append(delta)
                if on_text is not None:
                    on_text(delta)
            raw_final = _get_final_message(stream)

        if raw_final is not None:
            usage = getattr(raw_final, "usage", None)
            if usage is not None:
                tokens_in = int(getattr(usage, "input_tokens", 0) or 0)
                tokens_out = int(getattr(usage, "output_tokens", 0) or 0)

        latency_ms = int((time.perf_counter() - started) * 1000)
        return AdapterResponse(
            text="".join(final_text_parts),
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            raw=raw_final,
        )


def _iter_text(stream: Any) -> Iterator[str]:
    text_stream = getattr(stream, "text_stream", None)
    if text_stream is None:
        return iter(())
    return iter(text_stream)


def _get_final_message(stream: Any) -> Any:
    fn = getattr(stream, "get_final_message", None)
    if fn is None:
        return None
    return fn()
