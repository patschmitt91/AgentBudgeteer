"""Tests for the policy learning harness."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("sklearn")

from budgeteer.learning import (  # noqa: E402
    FEATURE_COLUMNS,
    LabeledExample,
    load_examples,
    train_policy,
)
from budgeteer.policy import Policy  # noqa: E402
from budgeteer.types import Features  # noqa: E402

POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "policy.yaml"
BENCH_RESULTS = Path(__file__).resolve().parents[1] / "bench" / "results.json"


def _defaults() -> object:
    return Policy.from_yaml(POLICY_PATH).defaults


def _mk(**overrides: object) -> Features:
    base: dict[str, object] = {
        "estimated_file_count": 1,
        "cross_file_dependency_score": 0.1,
        "test_presence": False,
        "type_safety_signal": False,
        "planning_depth_score": 1,
        "reasoning_vs_mechanical_score": 0.1,
        "estimated_input_tokens": 5_000,
    }
    base.update(overrides)
    return Features(**base)  # type: ignore[arg-type]


def _examples() -> list[LabeledExample]:
    return [
        LabeledExample(_mk(), "single"),
        LabeledExample(_mk(estimated_file_count=2), "single"),
        LabeledExample(_mk(estimated_input_tokens=900_000), "single"),
        LabeledExample(
            _mk(planning_depth_score=7, reasoning_vs_mechanical_score=0.8),
            "pciv",
        ),
        LabeledExample(
            _mk(planning_depth_score=9, reasoning_vs_mechanical_score=0.7, test_presence=True),
            "pciv",
        ),
        LabeledExample(
            _mk(estimated_file_count=20, cross_file_dependency_score=0.1),
            "fleet",
        ),
        LabeledExample(
            _mk(estimated_file_count=40, cross_file_dependency_score=0.05),
            "fleet",
        ),
    ]


def test_train_policy_fits_and_predicts_all_classes() -> None:
    learned = train_policy(_examples(), _defaults())  # type: ignore[arg-type]
    # With distinct clusters, the tree should fit every training example.
    assert learned.report.train_accuracy == pytest.approx(1.0)
    assert learned.report.samples == 7
    assert set(learned.report.class_labels) == {"single", "pciv", "fleet"}
    assert sum(learned.report.feature_importances.values()) == pytest.approx(1.0, abs=1e-6)
    for col in learned.report.feature_importances:
        assert col in FEATURE_COLUMNS


def test_learned_policy_routes_with_defaults() -> None:
    learned = train_policy(_examples(), _defaults())  # type: ignore[arg-type]
    decision = learned.route(
        _mk(estimated_file_count=30, cross_file_dependency_score=0.0),
        budget_remaining=5.0,
        latency_target_seconds=600,
    )
    assert decision.strategy == "fleet"
    assert decision.reason == "learned_policy"
    assert decision.model  # resolved from defaults


def test_train_policy_rejects_unknown_labels() -> None:
    examples = [LabeledExample(_mk(), "magenta")]
    with pytest.raises(ValueError, match="unknown strategy labels"):
        train_policy(examples, _defaults())  # type: ignore[arg-type]


def test_train_policy_rejects_empty_examples() -> None:
    with pytest.raises(ValueError, match="zero examples"):
        train_policy([], _defaults())  # type: ignore[arg-type]


def test_load_examples_reads_bench_results_shape(tmp_path: Path) -> None:
    if not BENCH_RESULTS.is_file():
        pytest.skip("bench/results.json not generated yet")
    examples = load_examples(BENCH_RESULTS)
    assert len(examples) == 10
    labels = {e.label for e in examples}
    assert labels.issubset({"single", "pciv", "fleet"})


def test_load_examples_accepts_flat_list(tmp_path: Path) -> None:
    payload = [
        {
            "features": _mk().model_dump(),
            "label": "single",
        },
        {
            "features": _mk(planning_depth_score=8).model_dump(),
            "label": "pciv",
        },
    ]
    path = tmp_path / "train.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    examples = load_examples(path)
    assert [e.label for e in examples] == ["single", "pciv"]


def test_load_examples_accepts_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "train.jsonl"
    lines = [
        json.dumps({"features": _mk().model_dump(), "label": "single"}),
        "",
        json.dumps({"features": _mk(estimated_file_count=20).model_dump(), "label": "fleet"}),
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    examples = load_examples(path)
    assert [e.label for e in examples] == ["single", "fleet"]


def test_load_examples_rejects_unknown_shape(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"results_maybe": 1}), encoding="utf-8")
    with pytest.raises(ValueError, match="unrecognized training payload"):
        load_examples(path)
