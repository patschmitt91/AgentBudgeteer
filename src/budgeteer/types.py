"""Shared pydantic types used by the router, strategies, and adapters."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class RepoSnapshot(BaseModel):
    """A minimal view of the repository the task will run against.

    The snapshot is intentionally cheap to compute. Strategies may do deeper
    inspection at execution time.
    """

    root: Path
    file_count: int = 0
    total_bytes: int = 0
    has_tests: bool = False
    has_type_config: bool = False
    languages: list[str] = Field(default_factory=list)


class Features(BaseModel):
    """Feature vector extracted from the task description and repo."""

    estimated_file_count: int
    cross_file_dependency_score: float = Field(ge=0.0, le=1.0)
    test_presence: bool
    type_safety_signal: bool
    planning_depth_score: int = Field(ge=0)
    reasoning_vs_mechanical_score: float = Field(ge=0.0, le=1.0)
    estimated_input_tokens: int = Field(ge=0)


class Task(BaseModel):
    """A unit of work handed to the router."""

    description: str
    repo: RepoSnapshot


class ExecutionContext(BaseModel):
    """Runtime context passed into a Strategy."""

    budget_remaining: float = Field(gt=0.0)
    latency_target_seconds: int = Field(gt=0)
    repo_snapshot: RepoSnapshot
    features: Features


class ModelInvocation(BaseModel):
    """One call to a model, recorded for cost and audit."""

    model: str
    role: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0


class StrategyResult(BaseModel):
    """Uniform result returned by every Strategy."""

    model_config = ConfigDict(protected_namespaces=())

    success: bool
    cost_usd: float
    latency_seconds: float
    artifacts: list[Path] = Field(default_factory=list)
    strategy_used: str
    model_trace: list[ModelInvocation] = Field(default_factory=list)
    error: str | None = None
    output_text: str = ""
