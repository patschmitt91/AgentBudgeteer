"""Generate the locked v0.3 SWE-bench Verified instance ID list.

Run this once from PCIV's venv (which has ``datasets`` installed):

    cd ../PCIV
    uv run python ../AgentBudgeteer/bench/swe_bench/generate_instance_list.py

The output (``instances_v0_3.txt``) is committed and treated as
immutable. Re-sampling for a different N opens a new file
(``instances_v0_4.txt``); never edit this one.

Sampling is deterministic: sort by instance_id, then
``random.Random(seed).sample(sorted_ids, n)``. Same output on any
machine with the same dataset version cached.
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

V0_3_SEED = 20260429
V0_3_N = 30


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=V0_3_SEED)
    parser.add_argument("--n", type=int, default=V0_3_N)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parent / "instances_v0_3.txt",
    )
    args = parser.parse_args(argv)

    try:
        from datasets import load_dataset
    except ImportError:
        print(
            "datasets package not available. Run from PCIV's venv:\n"
            "  cd ../PCIV && uv run python ../AgentBudgeteer/bench/swe_bench/"
            "generate_instance_list.py",
            file=sys.stderr,
        )
        return 2

    ds = load_dataset("princeton-nlp/SWE-bench_Verified", split="test")
    all_ids = sorted(str(row["instance_id"]) for row in ds)
    print(f"loaded {len(all_ids)} instance ids", file=sys.stderr)

    sampled = random.Random(args.seed).sample(all_ids, args.n)
    sampled.sort()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(sampled) + "\n", encoding="utf-8")
    print(f"wrote {len(sampled)} instance ids to {args.out}", file=sys.stderr)
    print(f"seed={args.seed} dataset=princeton-nlp/SWE-bench_Verified split=test", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
