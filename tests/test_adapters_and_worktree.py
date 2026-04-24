"""Additional coverage for small adapter and worktree helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from budgeteer.adapters.anthropic_adapter import (
    AdapterMessage,
    AnthropicAdapter,
    _get_final_message,
    _iter_text,
)
from budgeteer.fleet.worktree import (
    GitWorktreeManager,
    TempDirWorktreeManager,
    _detect_git_repo,
)


class _FakeStream:
    def __init__(self, chunks: list[str]) -> None:
        self.text_stream = iter(chunks)
        self._final = type(
            "M",
            (),
            {"usage": type("U", (), {"input_tokens": 42, "output_tokens": 17})()},
        )()

    def __enter__(self) -> _FakeStream:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def get_final_message(self) -> Any:
        return self._final


class _FakeMessages:
    def stream(self, **_: Any) -> _FakeStream:
        return _FakeStream(["hello ", "world"])


class _FakeAnthropic:
    def __init__(self) -> None:
        self.messages = _FakeMessages()


def test_anthropic_adapter_init_without_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        AnthropicAdapter()


def test_anthropic_adapter_streams_and_reports_tokens() -> None:
    adapter = AnthropicAdapter(client=_FakeAnthropic())
    received: list[str] = []
    resp = adapter.get_response(
        [
            AdapterMessage(role="system", content="sys1"),
            AdapterMessage(role="system", content="sys2"),
            AdapterMessage(role="user", content="u"),
        ],
        model="claude-x",
        max_tokens=128,
        on_text=received.append,
    )
    assert resp.text == "hello world"
    assert resp.tokens_in == 42
    assert resp.tokens_out == 17
    assert received == ["hello ", "world"]


def test_anthropic_adapter_explicit_system_wins() -> None:
    adapter = AnthropicAdapter(client=_FakeAnthropic())
    resp = adapter.get_response(
        [AdapterMessage(role="user", content="hi")],
        model="m",
        max_tokens=16,
        system="explicit",
    )
    assert resp.text == "hello world"


def test_iter_text_and_final_message_handle_missing_attrs() -> None:
    class _Bare:
        pass

    assert list(_iter_text(_Bare())) == []
    assert _get_final_message(_Bare()) is None


def test_temp_dir_worktree_manager_roundtrip() -> None:
    mgr = TempDirWorktreeManager()
    p = mgr.provision("run-id-1234", "w0")
    assert p.exists()
    mgr.cleanup(p)
    assert not p.exists()


def test_detect_git_repo_false_on_nonexistent(tmp_path: Path) -> None:
    assert _detect_git_repo(tmp_path / "missing") is False


def test_git_worktree_manager_falls_back_when_not_repo(tmp_path: Path) -> None:
    mgr = GitWorktreeManager(tmp_path)
    assert mgr.is_git is False
    p = mgr.provision("runabc123", "w0")
    assert p.exists()
    mgr.cleanup(p)
    assert not p.exists()


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def test_git_worktree_manager_real_git_repo(tmp_path: Path) -> None:
    # Skip if git is unavailable on PATH.
    try:
        subprocess.run(["git", "--version"], check=True, capture_output=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        pytest.skip("git not available")

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("hi\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")

    mgr = GitWorktreeManager(repo)
    assert mgr.is_git is True
    path = mgr.provision("runabcdef01", "w0")
    assert path.exists()
    assert (path / "a.txt").exists()
    mgr.cleanup(path)
