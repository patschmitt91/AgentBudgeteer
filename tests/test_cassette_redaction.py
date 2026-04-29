"""Regression tests for cassette redaction at write time (v0.3 TODO #1b).

The recording adapter must not leak secret-shaped strings (sk- keys,
bearer tokens, JWTs, hex blobs, env-var literal values) into a committed
cassette. ``Cassette.save`` runs every string leaf through
``agentcore.redaction.redact`` as the single chokepoint.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_DIR = Path(__file__).resolve().parents[1]
if str(REPO_DIR) not in sys.path:  # pragma: no cover - import side effect
    sys.path.insert(0, str(REPO_DIR))

from bench.live.cassette import (  # noqa: E402
    Cassette,
    CassetteCall,
    CassetteTotals,
    new_cassette,
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_save_redacts_sk_key_in_message_content(tmp_path: Path) -> None:
    cassette = new_cassette(task_id="t", provider="anthropic", model="m")
    cassette.calls.append(
        CassetteCall(
            request={
                "messages": [
                    {"role": "user", "content": "use sk-abcdef0123456789ABCDEFG to call"},
                ],
                "model": "m",
                "max_tokens": 16,
            },
            response={
                "text": "ok",
                "model": "m",
                "tokens_in": 5,
                "tokens_out": 1,
                "latency_ms": 12,
            },
        )
    )
    out = tmp_path / "cassette.json"
    cassette.save(out)

    text = _read(out)
    assert "sk-abcdef0123456789ABCDEFG" not in text
    assert "[REDACTED]" in text


def test_save_redacts_bearer_token_in_response_text(tmp_path: Path) -> None:
    cassette = new_cassette(task_id="t", provider="anthropic", model="m")
    cassette.calls.append(
        CassetteCall(
            request={"messages": [{"role": "user", "content": "hi"}], "model": "m", "max_tokens": 8},
            response={
                "text": "Authorization: Bearer abcDEF1234567890token",
                "model": "m",
                "tokens_in": 1,
                "tokens_out": 6,
                "latency_ms": 7,
            },
        )
    )
    out = tmp_path / "cassette.json"
    cassette.save(out)

    text = _read(out)
    assert "abcDEF1234567890token" not in text
    assert "[REDACTED]" in text


def test_save_redacts_env_var_literal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A literal value held in a secret-bearing env var must be scrubbed."""

    secret = "supersecret-value-NotInRegexCatalogue-9876"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    # Force the redaction module to refresh its env cache for this test.
    from agentcore.redaction import refresh_env_cache

    refresh_env_cache()

    cassette = new_cassette(task_id="t", provider="anthropic", model="m")
    cassette.calls.append(
        CassetteCall(
            request={
                "messages": [{"role": "user", "content": f"key was {secret}"}],
                "model": "m",
                "max_tokens": 8,
            },
            response={
                "text": "ok",
                "model": "m",
                "tokens_in": 4,
                "tokens_out": 1,
                "latency_ms": 3,
            },
        )
    )
    out = tmp_path / "cassette.json"
    cassette.save(out)

    text = _read(out)
    assert secret not in text


def test_save_preserves_non_secret_content(tmp_path: Path) -> None:
    """Plain strings round-trip: redaction is conservative."""

    cassette = new_cassette(task_id="t", provider="anthropic", model="m")
    cassette.calls.append(
        CassetteCall(
            request={
                "messages": [{"role": "user", "content": "reverse 'hello'"}],
                "model": "m",
                "max_tokens": 16,
            },
            response={
                "text": "def reverse(s): return s[::-1]",
                "model": "m",
                "tokens_in": 5,
                "tokens_out": 9,
                "latency_ms": 100,
            },
        )
    )
    out = tmp_path / "cassette.json"
    cassette.save(out)

    payload = json.loads(_read(out))
    assert payload["calls"][0]["request"]["messages"][0]["content"] == "reverse 'hello'"
    assert payload["calls"][0]["response"]["text"] == "def reverse(s): return s[::-1]"


def test_save_preserves_numeric_and_structural_fields(tmp_path: Path) -> None:
    """Redaction only touches strings; ints and floats pass through."""

    cassette = Cassette(
        task_id="t",
        provider="anthropic",
        model="m",
        recorded_at="2026-04-29T00:00:00+00:00",
        calls=[
            CassetteCall(
                request={"messages": [{"role": "user", "content": "x"}], "model": "m", "max_tokens": 32},
                response={
                    "text": "y",
                    "model": "m",
                    "tokens_in": 100,
                    "tokens_out": 200,
                    "latency_ms": 1234,
                },
            )
        ],
        totals=CassetteTotals(calls=1, tokens_in=100, tokens_out=200, cost_usd=0.001),
    )
    out = tmp_path / "cassette.json"
    cassette.save(out)

    payload = json.loads(_read(out))
    assert payload["calls"][0]["response"]["tokens_in"] == 100
    assert payload["calls"][0]["response"]["tokens_out"] == 200
    assert payload["calls"][0]["response"]["latency_ms"] == 1234
    assert payload["totals"]["cost_usd"] == 0.001
    assert payload["schema_version"] == 1
