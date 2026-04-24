"""Policy learning harness.

Trains a ``sklearn.tree.DecisionTreeClassifier`` to predict the expected
strategy from a feature vector, using labeled data from bench results or
a JSONL file of past executions.

This is the v1 successor to the hand-tuned decision tree in
``policy.py``. Training data format is a list of dicts with:

    {
      "features": { ... Features.model_dump() ... },
      "label": "single" | "pciv" | "fleet"
    }

Bench ``results.json`` files use the ``expected_strategy`` as the label
and ``features`` as the feature vector, so they are accepted directly.

The trained classifier can be inspected (feature importances, tree
structure) or called via ``LearnedPolicy.route`` to produce a
``RouteDecision`` compatible with the existing Router wiring.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from budgeteer.policy import ModelDefaults, RouteDecision
from budgeteer.types import Features

if TYPE_CHECKING:  # pragma: no cover - import only for type hints
    from sklearn.tree import DecisionTreeClassifier

FEATURE_COLUMNS: tuple[str, ...] = (
    "estimated_file_count",
    "cross_file_dependency_score",
    "test_presence",
    "type_safety_signal",
    "planning_depth_score",
    "reasoning_vs_mechanical_score",
    "estimated_input_tokens",
    "budget_remaining_usd",
    "latency_target_seconds",
)

STRATEGY_LABELS: tuple[str, ...] = ("single", "pciv", "fleet")

# Defaults for labeled rows that omit budget / latency. Chosen so the
# trained tree can still split on them sensibly: the mid-range budget and
# the long-form latency target used in bench tasks.
_DEFAULT_BUDGET_REMAINING_USD: float = 5.0
_DEFAULT_LATENCY_TARGET_SECONDS: float = 600.0


@dataclass
class LabeledExample:
    features: Features
    label: str
    budget_remaining_usd: float = _DEFAULT_BUDGET_REMAINING_USD
    latency_target_seconds: float = _DEFAULT_LATENCY_TARGET_SECONDS


@dataclass
class TrainingReport:
    samples: int
    label_counts: dict[str, int]
    train_accuracy: float
    feature_importances: dict[str, float]
    tree_depth: int
    leaf_count: int
    class_labels: list[str] = field(default_factory=list)


class LearnedPolicy:
    """Wraps a fitted sklearn tree and maps predictions to RouteDecision.

    The learned model predicts strategy only. Model selection still comes
    from ``ModelDefaults`` so the learning layer stays orthogonal to
    pricing and backend choices.
    """

    def __init__(
        self,
        classifier: DecisionTreeClassifier,
        defaults: ModelDefaults,
        report: TrainingReport,
    ) -> None:
        self._clf = classifier
        self._defaults = defaults
        self._report = report

    @property
    def report(self) -> TrainingReport:
        return self._report

    def route(
        self,
        features: Features,
        budget_remaining: float,
        latency_target_seconds: int,
    ) -> RouteDecision:
        """Predict a strategy from the full feature vector.

        Uses the same seven task features as the hand-tuned policy plus
        ``budget_remaining`` and ``latency_target_seconds`` so the learned
        tree can reproduce the hand-tuned guardrails (tight budget, short
        latency) when training data covers those regions.
        """

        row = _features_to_row(
            features,
            budget_remaining_usd=float(budget_remaining),
            latency_target_seconds=float(latency_target_seconds),
        )
        prediction = str(self._clf.predict([row])[0])
        model = _model_for(prediction, self._defaults)
        return RouteDecision(
            strategy=prediction,
            model=model,
            reason="learned_policy",
        )


def load_examples(path: Path) -> list[LabeledExample]:
    """Load labeled training data from a JSON or JSONL file.

    Accepted shapes:
      * bench results.json: top-level ``{"results": [{"features": ..., "expected_strategy": ...}]}``
      * flat list: ``[{"features": ..., "label": ...}]``
      * JSONL: one labeled example per line
    """

    text = path.read_text(encoding="utf-8")
    examples: list[LabeledExample] = []

    if path.suffix == ".jsonl":
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            examples.append(_coerce_example(json.loads(line)))
        return examples

    payload = json.loads(text)
    rows: list[dict[str, Any]]
    if isinstance(payload, dict) and "results" in payload:
        rows = list(payload["results"])
    elif isinstance(payload, list):
        rows = payload
    else:
        raise ValueError(
            f"unrecognized training payload shape in {path}: "
            "expected list, JSONL, or bench results.json"
        )
    for row in rows:
        examples.append(_coerce_example(row))
    return examples


def train_policy(
    examples: list[LabeledExample],
    defaults: ModelDefaults,
    *,
    max_depth: int = 5,
    min_samples_leaf: int = 1,
    random_state: int = 0,
) -> LearnedPolicy:
    """Fit a DecisionTreeClassifier on the labeled examples."""

    if not examples:
        raise ValueError("cannot train on zero examples")
    unknown = [e.label for e in examples if e.label not in STRATEGY_LABELS]
    if unknown:
        raise ValueError(
            f"unknown strategy labels in training data: {sorted(set(unknown))}. "
            f"expected one of {STRATEGY_LABELS}"
        )

    # Imported lazily so the learn harness is optional.
    from sklearn.tree import DecisionTreeClassifier  # noqa: PLC0415

    rows = [
        _features_to_row(
            e.features,
            budget_remaining_usd=e.budget_remaining_usd,
            latency_target_seconds=e.latency_target_seconds,
        )
        for e in examples
    ]
    labels = [e.label for e in examples]
    clf = DecisionTreeClassifier(
        max_depth=max_depth,
        min_samples_leaf=min_samples_leaf,
        random_state=random_state,
    )
    clf.fit(rows, labels)

    label_counts: dict[str, int] = {}
    for label in labels:
        label_counts[label] = label_counts.get(label, 0) + 1

    importances = {
        col: float(score)
        for col, score in zip(FEATURE_COLUMNS, clf.feature_importances_, strict=False)
    }

    report = TrainingReport(
        samples=len(examples),
        label_counts=label_counts,
        train_accuracy=float(clf.score(rows, labels)),
        feature_importances=importances,
        tree_depth=int(clf.get_depth()),
        leaf_count=int(clf.get_n_leaves()),
        class_labels=[str(c) for c in clf.classes_],
    )
    return LearnedPolicy(clf, defaults, report)


def _features_to_row(
    features: Features,
    *,
    budget_remaining_usd: float = _DEFAULT_BUDGET_REMAINING_USD,
    latency_target_seconds: float = _DEFAULT_LATENCY_TARGET_SECONDS,
) -> list[float]:
    return [
        float(features.estimated_file_count),
        float(features.cross_file_dependency_score),
        float(features.test_presence),
        float(features.type_safety_signal),
        float(features.planning_depth_score),
        float(features.reasoning_vs_mechanical_score),
        float(features.estimated_input_tokens),
        float(budget_remaining_usd),
        float(latency_target_seconds),
    ]


def _model_for(strategy: str, defaults: ModelDefaults) -> str:
    if strategy == "single":
        return defaults.single_agent_primary
    if strategy == "pciv":
        return defaults.pciv_planner
    if strategy == "fleet":
        return defaults.fleet_worker
    raise ValueError(f"unknown strategy {strategy!r}")


def _coerce_example(row: dict[str, Any]) -> LabeledExample:
    raw_features = row.get("features")
    if not isinstance(raw_features, dict):
        raise ValueError(f"row missing 'features' dict: {row!r}")
    features = Features(**raw_features)
    label = row.get("label") or row.get("expected_strategy") or row.get("strategy")
    if not isinstance(label, str):
        raise ValueError(f"row missing string label: {row!r}")
    budget = row.get("budget_remaining_usd")
    latency = row.get("latency_target_seconds")
    return LabeledExample(
        features=features,
        label=label,
        budget_remaining_usd=(
            float(budget) if isinstance(budget, int | float) else _DEFAULT_BUDGET_REMAINING_USD
        ),
        latency_target_seconds=(
            float(latency) if isinstance(latency, int | float) else _DEFAULT_LATENCY_TARGET_SECONDS
        ),
    )


__all__ = [
    "FEATURE_COLUMNS",
    "STRATEGY_LABELS",
    "LabeledExample",
    "LearnedPolicy",
    "TrainingReport",
    "load_examples",
    "train_policy",
]
