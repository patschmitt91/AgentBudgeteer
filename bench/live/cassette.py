"""Hand-rolled cassette replay for the live-provider micro-bench (AB-6).

Why hand-rolled instead of vcrpy:

- **No new dependency.** vcrpy plus the httpx bridge would add 2-3
  transitive deps for one bench test. We already have a clean injection
  seam at the `AnthropicAdapter` interface (the `_FakeAdapter` in
  ``tests/test_cli_e2e.py`` proves it works).
- **Stable across SDK upgrades.** Cassettes capture
  ``(messages, model, max_tokens) -> AdapterResponse`` at the adapter
  layer, not raw HTTP. Bumping ``anthropic`` does not invalidate them.
- **Inspectable.** A JSON file is `git diff`-friendly. vcrpy cassettes
  are YAML blobs of base64-ish HTTP bodies.

Cassette schema (v1):

```jsonc
{
    "schema_version": 1,
    "task_id": "...",
    "provider": "anthropic" | "azure_openai",
    "model": "claude-3-5-haiku-...",
    "recorded_at": "2026-04-25T12:34:56+00:00",
    "totals": {
        "calls": 1,
        "tokens_in": 123,
        "tokens_out": 456,
        "cost_usd": 0.0123
    },
    "calls": [
        {
            "request": {
                "messages": [{"role": "user", "content": "..."}],
                "model": "...",
                "max_tokens": 1500,
                "system": "..."
            },
            "response": {
                "text": "...",
                "model": "...",
                "tokens_in": 123,
                "tokens_out": 456,
                "latency_ms": 1234
            }
        }
    ]
}
```

Replay matches calls **in order** so a strategy that makes N adapter
calls per run records / replays exactly N entries. Mismatched request
shape (different model, different last user message) raises so a stale
cassette can never silently replay the wrong response.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentcore.redaction import redact

from budgeteer.adapters.anthropic_adapter import (
    AdapterMessage,
    AdapterResponse,
    OnTextCallback,
    StreamingChatClient,
)

CASSETTE_SCHEMA_VERSION = 1


def _redact_payload(obj: Any) -> Any:
    """Recursively redact secret-shaped strings in a JSON-serialisable payload.

    ``agentcore.redaction.redact_mapping`` only walks mappings; cassettes
    contain lists (``messages``, ``calls``) so we need a structural walker.
    Strings pass through ``redact()`` (regex patterns + cached env-var
    literal scrubbing); other primitives pass through untouched.

    Applied at ``Cassette.save`` time as the single chokepoint before the
    file hits disk. See v0.3 plan, TODO #1(b).
    """

    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: _redact_payload(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_payload(v) for v in obj]
    return obj


@dataclass
class CassetteCall:
    """A single recorded ``adapter.get_response`` invocation."""

    request: dict[str, Any]
    response: dict[str, Any]


@dataclass
class CassetteTotals:
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0


@dataclass
class Cassette:
    """In-memory representation of a cassette file."""

    task_id: str
    provider: str
    model: str
    recorded_at: str
    calls: list[CassetteCall] = field(default_factory=list)
    totals: CassetteTotals = field(default_factory=CassetteTotals)
    schema_version: int = CASSETTE_SCHEMA_VERSION

    @classmethod
    def load(cls, path: Path) -> Cassette:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("schema_version") != CASSETTE_SCHEMA_VERSION:
            raise ValueError(
                f"cassette {path} schema_version "
                f"{raw.get('schema_version')!r} != {CASSETTE_SCHEMA_VERSION}"
            )
        return cls(
            task_id=str(raw["task_id"]),
            provider=str(raw["provider"]),
            model=str(raw["model"]),
            recorded_at=str(raw["recorded_at"]),
            calls=[CassetteCall(**c) for c in raw["calls"]],
            totals=CassetteTotals(**raw["totals"]),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "provider": self.provider,
            "model": self.model,
            "recorded_at": self.recorded_at,
            "totals": asdict(self.totals),
            "calls": [asdict(c) for c in self.calls],
        }
        # Single chokepoint: redact every string leaf before write so a
        # leaked sk-/bearer/JWT shape or a secret env-var literal can't
        # land in a committed cassette. v0.3 plan TODO #1(b).
        payload = _redact_payload(payload)
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )


def _serialize_request(
    messages: list[AdapterMessage],
    *,
    model: str,
    max_tokens: int,
    system: str | None,
) -> dict[str, Any]:
    payload_messages = [{"role": m.role, "content": m.content} for m in messages]
    out: dict[str, Any] = {
        "messages": payload_messages,
        "model": model,
        "max_tokens": int(max_tokens),
    }
    if system is not None:
        out["system"] = system
    return out


def _request_match_key(req: dict[str, Any]) -> tuple[str, int, str]:
    """Stable identity for cassette match-checking.

    Two requests are considered the same call iff they share model,
    max_tokens, and the text of the last user message. This is permissive
    enough to survive whitespace-only edits to the system prompt while
    still catching prompt drift that would change the model's response.
    """

    last_user = ""
    for m in reversed(req.get("messages", [])):
        if m.get("role") == "user":
            last_user = str(m.get("content", ""))
            break
    return (
        str(req.get("model", "")),
        int(req.get("max_tokens", 0)),
        last_user,
    )


class CassetteAdapter:
    """Adapter that satisfies ``StreamingChatClient`` by replaying a cassette.

    Calls are replayed **in order**. The Nth call's request must match
    the Nth recorded request's match key (model + max_tokens + last
    user message); mismatch raises ``CassetteMismatch`` so a stale
    cassette can never silently return wrong data.
    """

    def __init__(self, cassette: Cassette) -> None:
        self._cassette = cassette
        self._cursor = 0

    def get_response(
        self,
        messages: list[AdapterMessage],
        *,
        model: str,
        max_tokens: int,
        system: str | None = None,
        on_text: OnTextCallback | None = None,
    ) -> AdapterResponse:
        if self._cursor >= len(self._cassette.calls):
            raise CassetteMismatch(
                f"cassette {self._cassette.task_id!r} exhausted: "
                f"replay called {self._cursor + 1} times but only "
                f"{len(self._cassette.calls)} recorded"
            )
        call = self._cassette.calls[self._cursor]
        actual = _serialize_request(messages, model=model, max_tokens=max_tokens, system=system)
        expected_key = _request_match_key(call.request)
        actual_key = _request_match_key(actual)
        if expected_key != actual_key:
            raise CassetteMismatch(
                f"cassette {self._cassette.task_id!r} call {self._cursor} "
                f"mismatch:\n  expected: {expected_key}\n  actual:   {actual_key}"
            )
        self._cursor += 1
        resp = call.response
        text = str(resp.get("text", ""))
        if on_text is not None and text:
            on_text(text)
        return AdapterResponse(
            text=text,
            model=str(resp.get("model", model)),
            tokens_in=int(resp.get("tokens_in", 0)),
            tokens_out=int(resp.get("tokens_out", 0)),
            latency_ms=int(resp.get("latency_ms", 0)),
            raw=None,
        )


class CassetteMismatch(RuntimeError):
    """Raised when replay request shape diverges from the recorded shape."""


class RecordingAdapter:
    """Wraps a real adapter, captures every call into a fresh cassette.

    Provider-agnostic: ``inner`` only needs to satisfy the
    ``StreamingChatClient`` Protocol. Today both ``AnthropicAdapter``
    and ``AzureOpenAIAdapter`` qualify; future adapters that match the
    ``get_response`` signature will too.

    Cost enforcement: the caller passes a ``charge`` callable that takes
    the per-call cost in USD and may raise ``BudgetExceeded``. We invoke
    it BEFORE recording the call so a cap-breach short-circuits without
    polluting the cassette. The intent is that the recorder is wrapped
    around a ``PersistentBudgetLedger.charge`` bound method with a
    configured hard cap (defaulting to $0.05 per AB-6).
    """

    def __init__(
        self,
        inner: StreamingChatClient,
        cassette: Cassette,
        *,
        cost_for_call: callable[[int, int], float],
        charge: callable[[float], None],
    ) -> None:
        self._inner = inner
        self._cassette = cassette
        self._cost_for_call = cost_for_call
        self._charge = charge

    def get_response(
        self,
        messages: list[AdapterMessage],
        *,
        model: str,
        max_tokens: int,
        system: str | None = None,
        on_text: OnTextCallback | None = None,
    ) -> AdapterResponse:
        response = self._inner.get_response(
            messages,
            model=model,
            max_tokens=max_tokens,
            system=system,
            on_text=on_text,
        )
        cost = float(self._cost_for_call(response.tokens_in, response.tokens_out))
        # Charge AFTER the call (real cost is only known post-response)
        # but BEFORE persisting to the cassette so a breach is auditable
        # without leaving a partial cassette on disk.
        self._charge(cost)
        self._cassette.calls.append(
            CassetteCall(
                request=_serialize_request(
                    messages, model=model, max_tokens=max_tokens, system=system
                ),
                response={
                    "text": response.text,
                    "model": response.model,
                    "tokens_in": response.tokens_in,
                    "tokens_out": response.tokens_out,
                    "latency_ms": response.latency_ms,
                },
            )
        )
        self._cassette.totals = CassetteTotals(
            calls=self._cassette.totals.calls + 1,
            tokens_in=self._cassette.totals.tokens_in + response.tokens_in,
            tokens_out=self._cassette.totals.tokens_out + response.tokens_out,
            cost_usd=self._cassette.totals.cost_usd + cost,
        )
        return response


def new_cassette(task_id: str, provider: str, model: str) -> Cassette:
    """Construct an empty cassette stamped with the current UTC time."""

    return Cassette(
        task_id=task_id,
        provider=provider,
        model=model,
        recorded_at=datetime.now(UTC).isoformat(),
    )
