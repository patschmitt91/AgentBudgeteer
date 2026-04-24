# ADR-0001: Use a hand-tuned decision tree, not a classifier, for v0 routing

- **Status:** Accepted
- **Date:** 2026-04-24
- **Deciders:** @patschmitt91
- **Supersedes:** —

## Context

The router has to pick a strategy (`SingleAgent`, `PCIV`, `Fleet`) from
features extracted by the classifier. Two obvious candidates for the
selection layer:

1. A hand-tuned decision tree whose thresholds live in `config/policy.yaml`.
2. A learned classifier (a small sklearn tree, a logistic regression, or
   a distillation from an LLM judge).

v0 has no labeled training data, no live-provider bench results, and no
production traffic.

## Decision

Ship a hand-tuned decision tree as the v0 policy. Expose every threshold
in `config/policy.yaml`. Keep a `LearnedPolicy` code path (`learning.py`)
behind an optional extra (`pip install agent-budgeteer[learn]`), but do
not wire it into the default router.

## Consequences

### Positive

- **Readable and auditable.** A reviewer can read
  `config/policy.yaml` and explain every routing decision.
- **No training-data dependency.** v0 ships with zero labeled examples;
  a learned policy that trains on zero examples is worse than a tuned
  one.
- **Thresholds are the debugging surface.** When the router picks the
  wrong strategy, the fix is a single YAML edit.

### Negative

- **Bias of the author.** Hand-tuning encodes my intuitions about which
  tasks need which topology. These intuitions are untested.
- **Brittle at boundaries.** A task one token over a threshold gets a
  different strategy than one token under; the tree has no notion of
  uncertainty.

### Neutral

- `learning.py` stays on disk and stays tested. v0.2 swaps the default
  to `LearnedPolicy` once bench results exist to train on.

## Alternatives considered

### Train a small classifier on synthetic labels

Generate labels by running each strategy on each bench fixture and
labeling the winner. **Rejected** because `bench/` is dry-run in v0; we
have no live outcomes to label from. Doing this with synthetic outcomes
would train the classifier to mimic the hand-tuned tree, adding cost
without adding signal.

### Ask an LLM judge per task

At route time, ask a fast model which strategy to use. **Rejected**
because it adds a network call on the critical path of every run, it
cannot honor the budget cap (the judge's cost is not projectable until
after the call), and it replaces "explain why this task picked Fleet"
with "the judge said so."

## Validation

- `tests/test_policy.py` covers the decision tree branches.
- `bench/runner.py` exercises the tree against 10 fixture tasks and
  compares to expected strategies.
- The `LearnedPolicy` path is covered by `tests/test_learning.py` and
  kept green even though it is not wired into the default CLI.
