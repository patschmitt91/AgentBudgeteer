"""Tests for the SWE-bench problem-statement loader (handoff brief gap #1).

Validates ``load_problem_statements`` and the ``--problems-file`` flag
end-to-end through ``run(...)``. Covers happy path, missing-instance
fail-loud, malformed JSON, missing fields, and the stub fallback when
no file is provided.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_DIR = Path(__file__).resolve().parents[1]
if str(REPO_DIR) not in sys.path:  # pragma: no cover - import side effect
    sys.path.insert(0, str(REPO_DIR))

from bench.swe_bench.runner import (  # noqa: E402
    DEFAULT_INSTANCES_FILE,
    DEFAULT_POLICY_PATH,
    load_problem_statements,
    run,
)


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# load_problem_statements
# ---------------------------------------------------------------------------


def test_loader_happy_path(tmp_path: Path) -> None:
    """Records present for every requested instance return a complete dict."""

    path = tmp_path / "problems.jsonl"
    _write_jsonl(
        path,
        [
            {"instance_id": "django__django-1", "problem_statement": "fix foo"},
            {"instance_id": "django__django-2", "problem_statement": "fix bar"},
        ],
    )
    out = load_problem_statements(path, ["django__django-1", "django__django-2"])
    assert out == {
        "django__django-1": "fix foo",
        "django__django-2": "fix bar",
    }


def test_loader_extra_records_kept_silently(tmp_path: Path) -> None:
    """Records for non-requested instances are kept; not an error."""

    path = tmp_path / "problems.jsonl"
    _write_jsonl(
        path,
        [
            {"instance_id": "a", "problem_statement": "1"},
            {"instance_id": "b", "problem_statement": "2"},
            {"instance_id": "c", "problem_statement": "3"},
        ],
    )
    out = load_problem_statements(path, ["a", "b"])
    # Extra "c" is preserved; the same JSONL serves multiple sublists.
    assert out["a"] == "1"
    assert out["b"] == "2"
    assert out["c"] == "3"


def test_loader_missing_instance_fails_loud(tmp_path: Path) -> None:
    """Any requested instance not in the file raises ValueError."""

    path = tmp_path / "problems.jsonl"
    _write_jsonl(
        path, [{"instance_id": "a", "problem_statement": "1"}]
    )
    with pytest.raises(ValueError, match="missing problem_statement"):
        load_problem_statements(path, ["a", "b", "c"])


def test_loader_truncates_long_missing_list_in_message(tmp_path: Path) -> None:
    path = tmp_path / "problems.jsonl"
    _write_jsonl(path, [])
    # Zero-padded so lexicographic sort matches numeric order (the
    # error message sorts before truncating).
    requested = [f"id-{i:02d}" for i in range(20)]
    with pytest.raises(ValueError) as exc_info:
        load_problem_statements(path, requested)
    msg = str(exc_info.value)
    # First five missing IDs surfaced with an ellipsis hint.
    assert "id-00" in msg and "id-04" in msg
    assert " ..." in msg
    assert "20 requested instance" in msg


def test_loader_malformed_json_raises_with_lineno(tmp_path: Path) -> None:
    path = tmp_path / "problems.jsonl"
    path.write_text(
        '{"instance_id": "a", "problem_statement": "1"}\n'
        '{not json}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r":2:"):
        load_problem_statements(path, ["a"])


def test_loader_missing_fields_rejected(tmp_path: Path) -> None:
    path = tmp_path / "problems.jsonl"
    _write_jsonl(path, [{"instance_id": "a"}])  # no problem_statement
    with pytest.raises(ValueError, match="problem_statement"):
        load_problem_statements(path, ["a"])


def test_loader_empty_problem_statement_rejected(tmp_path: Path) -> None:
    path = tmp_path / "problems.jsonl"
    _write_jsonl(
        path, [{"instance_id": "a", "problem_statement": ""}]
    )
    with pytest.raises(ValueError, match="problem_statement"):
        load_problem_statements(path, ["a"])


def test_loader_skips_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "problems.jsonl"
    path.write_text(
        '\n'
        '{"instance_id": "a", "problem_statement": "1"}\n'
        '\n'
        '   \n',
        encoding="utf-8",
    )
    out = load_problem_statements(path, ["a"])
    assert out == {"a": "1"}


def test_loader_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_problem_statements(tmp_path / "nope.jsonl", [])


# ---------------------------------------------------------------------------
# run() integration
# ---------------------------------------------------------------------------


def test_run_uses_problems_file_when_provided(tmp_path: Path) -> None:
    """The first 2 instances from the v0.3 list pick up real problems."""

    instance_ids = (
        DEFAULT_INSTANCES_FILE.read_text(encoding="utf-8").splitlines()[:2]
    )
    problems = tmp_path / "problems.jsonl"
    _write_jsonl(
        problems,
        [
            {
                "instance_id": instance_ids[0],
                "problem_statement": f"REAL: {instance_ids[0]}",
            },
            {
                "instance_id": instance_ids[1],
                "problem_statement": f"REAL: {instance_ids[1]}",
            },
        ],
    )

    payload = run(
        instances_file=DEFAULT_INSTANCES_FILE,
        policy_path=DEFAULT_POLICY_PATH,
        out_dir=tmp_path / "results",
        n_instances=2,
        budget_cap_usd=2.00,
        latency_target_seconds=600,
        problems_file=problems,
    )

    # Confirm the harness ran 4 arms × 2 instances. The stub path
    # writes the prompt verbatim into the response; a successful smoke
    # only asserts the harness did not error.
    for arm in payload["arms"].values():
        assert len(arm["instances"]) == 2


def test_run_fails_loud_when_problems_file_incomplete(tmp_path: Path) -> None:
    """Missing instance in the problems file aborts before any arm runs."""

    problems = tmp_path / "problems.jsonl"
    _write_jsonl(
        problems,
        [{"instance_id": "not-in-the-list", "problem_statement": "x"}],
    )
    with pytest.raises(ValueError, match="missing problem_statement"):
        run(
            instances_file=DEFAULT_INSTANCES_FILE,
            policy_path=DEFAULT_POLICY_PATH,
            out_dir=tmp_path / "results",
            n_instances=2,
            budget_cap_usd=2.00,
            latency_target_seconds=600,
            problems_file=problems,
        )


def test_run_stub_fallback_when_no_problems_file(tmp_path: Path) -> None:
    """Default path: synthetic ``resolve {id}`` keeps existing CI behaviour."""

    payload = run(
        instances_file=DEFAULT_INSTANCES_FILE,
        policy_path=DEFAULT_POLICY_PATH,
        out_dir=tmp_path / "results",
        n_instances=1,
        budget_cap_usd=2.00,
        latency_target_seconds=600,
    )
    assert payload["manifest"]["mode"] == "stub"
    for arm in payload["arms"].values():
        assert len(arm["instances"]) == 1
