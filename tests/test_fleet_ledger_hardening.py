"""Phase 3 hardening regressions for AgentBudgeteer.

Storage: PRAGMA + ON DELETE CASCADE on the fleet ShardLedger.
Redaction: complete_shard / fail_shard scrub secrets at the boundary.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from budgeteer.fleet.ledger import ShardLedger


def _pragma(led: ShardLedger, name: str) -> object:
    return led._conn.execute(f"PRAGMA {name}").fetchone()[0]


def test_pragmas_set_on_connection(tmp_path: Path) -> None:
    led = ShardLedger(tmp_path / "p.db")
    assert str(_pragma(led, "journal_mode")).lower() == "wal"
    assert int(_pragma(led, "foreign_keys")) == 1
    assert int(_pragma(led, "busy_timeout")) == 5000
    assert int(_pragma(led, "user_version")) == 2
    led.close()


def test_cascade_delete_removes_shards(tmp_path: Path) -> None:
    led = ShardLedger(tmp_path / "c.db")
    led.record_run("rC", "task")
    led.add_shard("s1", "rC", "do thing")
    assert len(led.list_shards("rC")) == 1
    with led._lock:
        led._conn.execute("DELETE FROM runs WHERE run_id = ?", ("rC",))
    assert led.list_shards("rC") == []
    led.close()


def test_complete_shard_redacts_result_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "sk-leak-complete-abcdefghij1234567")
    from budgeteer.redaction import refresh_env_cache

    refresh_env_cache()

    led = ShardLedger(tmp_path / "r.db")
    led.record_run("rR", "task")
    led.add_shard("s1", "rR", "do thing")
    led.claim_next("rR", "w1")
    led.complete_shard(
        "s1",
        result_text="result with sk-leak-complete-abcdefghij1234567 inside",
        cost_usd=0.0,
        tokens_in=0,
        tokens_out=0,
        worktree_path=None,
    )
    [shard] = led.list_shards("rR")
    assert shard.result_text is not None
    assert "sk-leak-complete" not in shard.result_text
    assert "REDACTED" in shard.result_text
    led.close()


def test_fail_shard_redacts_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "sk-leak-fail-abcdefghij1234567")
    from budgeteer.redaction import refresh_env_cache

    refresh_env_cache()

    led = ShardLedger(tmp_path / "f.db")
    led.record_run("rF", "task")
    led.add_shard("s1", "rF", "do thing")
    led.claim_next("rF", "w1")
    led.fail_shard("s1", "boom: sk-leak-fail-abcdefghij1234567 trailing")
    [shard] = led.list_shards("rF")
    assert shard.error is not None
    assert "sk-leak-fail" not in shard.error
    assert "REDACTED" in shard.error
    led.close()
