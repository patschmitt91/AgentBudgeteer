"""CI smoke check: run the bench and fail if routing accuracy regresses.

Usage:
    uv run python -m bench.smoke

Runs the full runner in-process (no subprocess), compares the resulting
accuracy to ``bench/results.baseline.json``, and exits non-zero when
accuracy drops. Per-strategy matched counts must not regress either.
"""

from __future__ import annotations

import json
import sys

from bench.runner import BENCH_DIR, run_all

BASELINE_PATH = BENCH_DIR / "results.baseline.json"


def main() -> int:
    if not BASELINE_PATH.exists():
        print(f"baseline missing: {BASELINE_PATH}", file=sys.stderr)
        return 2

    with BASELINE_PATH.open("r", encoding="utf-8") as f:
        baseline = json.load(f)

    summary = run_all(BENCH_DIR / "tasks", budget=5.0)

    baseline_acc = float(baseline["accuracy"])
    current_acc = summary.accuracy
    if current_acc + 1e-9 < baseline_acc:
        print(
            f"routing accuracy regressed: {current_acc:.4f} < baseline {baseline_acc:.4f}",
            file=sys.stderr,
        )
        return 1

    baseline_by = baseline.get("by_strategy", {})
    for strategy, row in baseline_by.items():
        cur = summary.by_strategy.get(strategy, {"matched": 0, "mismatched": 0})
        if cur["matched"] < row["matched"]:
            print(
                f"routing regressed for strategy {strategy!r}: "
                f"matched {cur['matched']} < baseline {row['matched']}",
                file=sys.stderr,
            )
            return 1

    print(f"bench smoke ok: accuracy {current_acc:.4f} (baseline {baseline_acc:.4f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
