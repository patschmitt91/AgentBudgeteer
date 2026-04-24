"""Task feature extraction. All heuristics are regex or count based.

Wordlists default to the constants below but can be overridden via
``ClassifierConfig``, which ``Policy.from_yaml`` loads from the
``classifier`` block in ``config/policy.yaml``. That keeps routing
heuristics tunable without a code change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from budgeteer.types import Features, RepoSnapshot

_DEFAULT_REASONING_TOKENS: frozenset[str] = frozenset(
    {
        "design",
        "debug",
        "architect",
        "architecture",
        "analyze",
        "investigate",
        "refactor",
        "plan",
        "evaluate",
        "decide",
        "reason",
        "diagnose",
        "root-cause",
        "trade-off",
        "optimize",
    }
)

_DEFAULT_MECHANICAL_TOKENS: frozenset[str] = frozenset(
    {
        "rename",
        "move",
        "format",
        "translate",
        "copy",
        "delete",
        "add",
        "remove",
        "replace",
        "update",
        "generate",
        "extract",
        "convert",
        "lint",
        "reformat",
    }
)

_DEFAULT_IMPERATIVE_VERBS: frozenset[str] = frozenset(
    {
        "add",
        "remove",
        "rename",
        "create",
        "delete",
        "implement",
        "refactor",
        "write",
        "move",
        "replace",
        "update",
        "fix",
        "design",
        "plan",
        "document",
        "test",
        "verify",
        "migrate",
        "build",
        "convert",
        "generate",
        "analyze",
        "investigate",
        "evaluate",
        "decide",
        "debug",
    }
)


@dataclass(frozen=True)
class ClassifierConfig:
    """Tunable wordlists for the heuristic classifier."""

    reasoning_tokens: frozenset[str] = field(default_factory=lambda: _DEFAULT_REASONING_TOKENS)
    mechanical_tokens: frozenset[str] = field(default_factory=lambda: _DEFAULT_MECHANICAL_TOKENS)
    imperative_verbs: frozenset[str] = field(default_factory=lambda: _DEFAULT_IMPERATIVE_VERBS)

    @classmethod
    def default(cls) -> ClassifierConfig:
        return cls()


_FILE_PATH_PATTERN = re.compile(
    r"(?:[A-Za-z0-9_\-./]*/)?[A-Za-z0-9_\-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|cs|rb|cpp|c|h|hpp|md|yaml|yml|json|toml)",
)

_FILE_COUNT_HINT = re.compile(r"\b(\d{1,4})\s+(?:files?|modules?|packages?)\b", re.IGNORECASE)

_CLAUSE_SPLIT = re.compile(r"[.;\n]|(?:\s+and\s+)|(?:\s+then\s+)", re.IGNORECASE)

_WORD = re.compile(r"[A-Za-z][A-Za-z\-]+")


def _tokenize(text: str) -> list[str]:
    return [m.group(0).lower() for m in _WORD.finditer(text)]


def _estimate_files(description: str, snapshot: RepoSnapshot) -> int:
    paths = {m.group(0) for m in _FILE_PATH_PATTERN.finditer(description)}
    if paths:
        return len(paths)
    hint = _FILE_COUNT_HINT.search(description)
    if hint:
        try:
            return max(1, int(hint.group(1)))
        except ValueError:
            pass
    if re.search(r"\b(all|every|across|entire|whole)\b", description, re.IGNORECASE):
        return max(1, snapshot.file_count)
    return 1


def _cross_file_dependency_score(description: str, file_count: int) -> float:
    text = description.lower()
    signals = 0
    if re.search(r"\b(import|imports|depends on|dependency|circular)\b", text):
        signals += 2
    if re.search(r"\b(shared|common|coupled|tangled)\b", text):
        signals += 1
    if re.search(r"\b(independent|isolated|parallel|per-file)\b", text):
        signals -= 3
    base = 0.2
    if file_count >= 5:
        base += 0.2
    if file_count >= 15:
        base += 0.2
    score = base + 0.15 * signals
    return round(max(0.0, min(1.0, score)), 3)


def _planning_depth(description: str, imperative_verbs: frozenset[str]) -> int:
    depth = 0
    for raw_clause in _CLAUSE_SPLIT.split(description):
        clause = raw_clause.strip()
        if not clause:
            continue
        words = _tokenize(clause)
        if not words:
            continue
        if words[0] in imperative_verbs:
            depth += 1
            continue
        if any(w in imperative_verbs for w in words[:3]):
            depth += 1
    return depth


def _reasoning_vs_mechanical(
    description: str,
    reasoning_tokens: frozenset[str],
    mechanical_tokens: frozenset[str],
) -> float:
    tokens = _tokenize(description)
    if not tokens:
        return 0.0
    reasoning = sum(1 for t in tokens if t in reasoning_tokens)
    mechanical = sum(1 for t in tokens if t in mechanical_tokens)
    total = reasoning + mechanical
    if total == 0:
        return 0.0
    return reasoning / total


def _estimate_input_tokens(description: str, snapshot: RepoSnapshot) -> int:
    # ~4 characters per token is the industry rule of thumb for English.
    desc_tokens = max(1, len(description) // 4)
    repo_tokens = snapshot.total_bytes // 4
    return desc_tokens + repo_tokens


def extract_features(
    task: str,
    repo_snapshot: RepoSnapshot,
    config: ClassifierConfig | None = None,
) -> Features:
    """Derive a feature vector from the task description and a repo snapshot."""

    cfg = config or ClassifierConfig.default()
    file_count = _estimate_files(task, repo_snapshot)
    return Features(
        estimated_file_count=file_count,
        cross_file_dependency_score=_cross_file_dependency_score(task, file_count),
        test_presence=repo_snapshot.has_tests,
        type_safety_signal=repo_snapshot.has_type_config,
        planning_depth_score=_planning_depth(task, cfg.imperative_verbs),
        reasoning_vs_mechanical_score=_reasoning_vs_mechanical(
            task, cfg.reasoning_tokens, cfg.mechanical_tokens
        ),
        estimated_input_tokens=_estimate_input_tokens(task, repo_snapshot),
    )
