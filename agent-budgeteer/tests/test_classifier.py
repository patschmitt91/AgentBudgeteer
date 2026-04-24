"""Unit tests for the classifier heuristics."""

from __future__ import annotations

from pathlib import Path

from budgeteer.classifier import extract_features
from budgeteer.types import RepoSnapshot


def _snap(**kw: object) -> RepoSnapshot:
    defaults: dict[str, object] = {
        "root": Path("."),
        "file_count": 10,
        "total_bytes": 20_000,
        "has_tests": False,
        "has_type_config": False,
        "languages": [],
    }
    defaults.update(kw)
    return RepoSnapshot(**defaults)  # type: ignore[arg-type]


def test_file_count_from_explicit_paths() -> None:
    task = "Update src/users.py and src/accounts.py to share a helper in src/shared.py"
    f = extract_features(task, _snap())
    assert f.estimated_file_count == 3


def test_file_count_from_hint() -> None:
    task = "Reformat 12 files to match the new style"
    f = extract_features(task, _snap(file_count=50))
    assert f.estimated_file_count == 12


def test_file_count_all_scope_uses_snapshot() -> None:
    task = "Rename the User type across the entire repository"
    f = extract_features(task, _snap(file_count=23))
    assert f.estimated_file_count == 23


def test_cross_file_coupling_low_for_independent() -> None:
    task = "Independent per-file docstring cleanup across 20 files"
    f = extract_features(task, _snap(file_count=20))
    assert f.cross_file_dependency_score < 0.3


def test_cross_file_coupling_high_for_shared() -> None:
    task = "Refactor the shared helper that many imports depend on, fixing circular dependency"
    f = extract_features(task, _snap(file_count=15))
    assert f.cross_file_dependency_score > 0.4


def test_test_presence_and_type_signal_pass_through() -> None:
    task = "Fix the bug"
    f = extract_features(task, _snap(has_tests=True, has_type_config=True))
    assert f.test_presence is True
    assert f.type_safety_signal is True


def test_planning_depth_counts_imperative_clauses() -> None:
    task = (
        "Design a plan. Write a spec. Implement the module. Test the edges. "
        "Document the API. Verify with the reviewer."
    )
    f = extract_features(task, _snap())
    assert f.planning_depth_score >= 5


def test_reasoning_ratio_heavy() -> None:
    task = "Investigate, analyze, and debug the architecture to evaluate trade-offs"
    f = extract_features(task, _snap())
    assert f.reasoning_vs_mechanical_score >= 0.8


def test_reasoning_ratio_mechanical() -> None:
    task = "Rename foo to bar, move the file, reformat, and replace imports"
    f = extract_features(task, _snap())
    assert f.reasoning_vs_mechanical_score == 0.0


def test_estimated_tokens_scales_with_repo_bytes() -> None:
    task = "small task"
    small = extract_features(task, _snap(total_bytes=0))
    large = extract_features(task, _snap(total_bytes=2_400_000))
    assert large.estimated_input_tokens > small.estimated_input_tokens
    assert large.estimated_input_tokens >= 600_000
