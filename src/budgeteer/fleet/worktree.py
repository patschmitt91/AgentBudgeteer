"""Worktree provisioning for Fleet workers.

Default implementation uses `git worktree add`. If the repo is not a git
repo, falls back to isolated scratch directories so the strategy still
runs end-to-end on arbitrary filesystems. Tests inject a fake manager via
the `WorktreeManager` protocol.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Protocol


class WorktreeManager(Protocol):
    """Interface every worktree provider must satisfy."""

    def provision(self, run_id: str, worker_id: str) -> Path: ...

    def cleanup(self, path: Path) -> None: ...


class GitWorktreeManager:
    """Uses `git worktree add <path>` when the root is a git repo.

    Falls back to a fresh tempdir when git is unavailable or the root is
    not a repo. Cleanup removes the directory and detaches the worktree
    when git was used.
    """

    def __init__(self, repo_root: Path, base_branch: str | None = None) -> None:
        self._repo_root = repo_root
        self._base_branch = base_branch
        self._is_git = _detect_git_repo(repo_root)
        # Provisioned-path bookkeeping is touched by every worker thread.
        # Guard reads/writes with a lock so a failed provision cannot leave
        # a stale entry visible to a concurrent cleanup. See harden/phase-2
        # audit item #7.
        self._provisioned: dict[Path, bool] = {}
        self._lock = threading.Lock()

    @property
    def is_git(self) -> bool:
        return self._is_git

    def provision(self, run_id: str, worker_id: str) -> Path:
        workdir = Path(tempfile.mkdtemp(prefix=f"budgeteer-fleet-{run_id[:8]}-{worker_id}-"))
        if self._is_git:
            try:
                cmd = [
                    "git",
                    "-C",
                    str(self._repo_root),
                    "worktree",
                    "add",
                    "--detach",
                    str(workdir),
                ]
                if self._base_branch:
                    cmd.append(self._base_branch)
                subprocess.run(cmd, check=True, capture_output=True, text=True)
                with self._lock:
                    self._provisioned[workdir] = True
                return workdir
            except (subprocess.CalledProcessError, FileNotFoundError):
                # Fall through to tempdir-only mode.
                with self._lock:
                    self._provisioned[workdir] = False
                return workdir
        with self._lock:
            self._provisioned[workdir] = False
        return workdir

    def cleanup(self, path: Path) -> None:
        with self._lock:
            used_git = self._provisioned.pop(path, False)
        if used_git:
            try:
                subprocess.run(
                    ["git", "-C", str(self._repo_root), "worktree", "remove", "--force", str(path)],
                    check=False,
                    capture_output=True,
                    text=True,
                )
            except FileNotFoundError:
                pass
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)


class TempDirWorktreeManager:
    """Non-git fallback that provisions plain temp directories. Used by tests."""

    def __init__(self) -> None:
        self._paths: list[Path] = []

    def provision(self, run_id: str, worker_id: str) -> Path:
        p = Path(tempfile.mkdtemp(prefix=f"budgeteer-fleet-{run_id[:8]}-{worker_id}-"))
        self._paths.append(p)
        return p

    def cleanup(self, path: Path) -> None:
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        if path in self._paths:
            self._paths.remove(path)


def _detect_git_repo(root: Path) -> bool:
    if not root.exists():
        return False
    try:
        res = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return False
    return res.returncode == 0 and res.stdout.strip() == "true"
