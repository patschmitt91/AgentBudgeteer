# bench/swe_bench

SWE-bench Verified harness for the v0.3 four-arm comparison. See
[`docs/plans/v0.3-swe-bench-four-arm.md`](../../docs/plans/v0.3-swe-bench-four-arm.md)
for the full plan, and
[`docs/plans/v0.3-handoff-brief.md`](../../docs/plans/v0.3-handoff-brief.md)
for what's stubbed vs. what an Agent Framework engineer must wire to
run live.

## Files

- `instances_v0_3.txt` — **immutable.** Locked instance ID list for
  v0.3, sampled with `seed=20260429`, N=30 from
  `princeton-nlp/SWE-bench_Verified` test split. Sort order is
  lexicographic. Re-sampling for a different N (e.g. N=100 if v0.3
  results are noisy) opens a new file (`instances_v0_4.txt`); never
  edit this one.
- `generate_instance_list.py` — the (one-shot) sampler. Run from
  PCIV's venv since AgentBudgeteer doesn't depend on `datasets` at
  runtime.
- `runner.py` — four-arm harness. Stub-only today. `just bench-smoke`
  invokes it against the first 5 instance IDs.

## Why immutable?

Reproducibility. Every result reported in
`bench/results/<date>/REPORT.md` cites this file by name; mutating it
silently invalidates every prior run's claims.

## Distribution (v0.3, N=30)

| Project | N |
|---------|---|
| django | 16 |
| sympy | 8 |
| astropy | 2 |
| scikit-learn | 2 |
| pytest-dev | 1 |
| sphinx-doc | 1 |

The Django skew (53%) is a property of SWE-bench Verified itself
(Django dominates the dataset). Findings on Django-heavy results may
not generalise to the long-tail projects. This is an acknowledged
limitation in REPORT.md.
