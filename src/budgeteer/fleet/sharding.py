"""Shard planning: split a task description into independent units."""

from __future__ import annotations

import re
from collections.abc import Iterable

_FILE_PATH_PATTERN = re.compile(
    r"(?:[A-Za-z0-9_\-./]*/)?[A-Za-z0-9_\-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|cs|rb|cpp|c|h|hpp|md|yaml|yml|json|toml)",
)

_SENTENCE_SPLIT = re.compile(r"[.;\n]")


def plan_shards(task: str, min_shards: int = 1, max_shards: int = 16) -> list[str]:
    """Return one shard description per independent unit of work.

    Preference order:
      1. Extract file paths from the task. One shard per file.
      2. Split into sentences. One shard per sentence that looks actionable.
      3. Fall back to a single shard with the whole task.

    All outputs are clamped to `[min_shards, max_shards]`.
    """

    paths = _unique_preserve_order(m.group(0) for m in _FILE_PATH_PATTERN.finditer(task))
    if paths:
        shards = [_per_path_shard(task, p) for p in paths]
        return _clamp(shards, min_shards, max_shards)

    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(task) if s.strip()]
    sentences = [s for s in sentences if len(s) > 8]
    if len(sentences) >= 2:
        return _clamp(sentences, min_shards, max_shards)

    return [task.strip() or task]


def _per_path_shard(task: str, path: str) -> str:
    return f"Apply this task to {path}: {task.strip()}"


def _unique_preserve_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _clamp(items: list[str], min_shards: int, max_shards: int) -> list[str]:
    if not items:
        return items
    if len(items) < min_shards:
        while len(items) < min_shards:
            items.append(items[-1])
    if len(items) > max_shards:
        return items[:max_shards]
    return items
