# ADR-0003: Use git worktrees, not branches, for Fleet workers

- **Status:** Accepted
- **Date:** 2026-04-24
- **Deciders:** @patschmitt91
- **Supersedes:** —

## Context

The Fleet strategy runs N workers in parallel, each editing files in
the same repository. For this to be safe, each worker needs its own
filesystem view of the repo so that:

- Two workers editing the same file cannot interleave writes.
- A worker's in-progress edits cannot be read by another worker's tool
  calls.
- A worker's crash cannot leave the user's working tree in a dirty
  state.

Two git features address this:

1. A single working tree with per-worker branches (each worker checks
   out its branch, edits, commits, then checks out the next).
2. Per-worker git worktrees (`git worktree add`), where each worker
   gets its own directory rooted on a branch.

## Decision

Each fleet worker gets a dedicated git worktree under
`.budgeteer/worktrees/<run_id>/<shard_id>/` rooted on a dedicated
branch. The user's main working tree is never touched. When the repo
is not a git repo (e.g. a bare directory passed via `--repo`), workers
fall back to isolated temp directories seeded with a copy of the
source files.

## Consequences

### Positive

- **True parallelism.** Workers run concurrently with no per-file lock
  and no branch-switching serialization.
- **User's working tree is sacred.** The router never modifies the
  user's checkout; all edits happen in worktrees that can be inspected
  or discarded.
- **Crash recovery is trivial.** A failed worker leaves its worktree
  on disk. The user can `git -C .budgeteer/worktrees/<run>/<shard> diff`
  to inspect partial work; cleanup is `git worktree remove`.
- **Integration is a normal merge.** Each worker commits on its branch;
  a single integration step squash-merges the successful branches.
  No special "collect pending edits" protocol is needed.

### Negative

- **Disk cost.** N worktrees means roughly N × repo size on disk.
  For large repos this is non-trivial. Mitigated by worktrees sharing
  objects (git's default).
- **Fallback path exists.** When the target directory is not a git
  repo, we degrade to temp-dir copies; that path has to stay tested
  and produce the same `StrategyResult` shape.
- **Not portable to non-git VCS.** Mercurial, Pijul, etc. would need
  a different abstraction.

### Neutral

- Worktree cleanup is opt-in (`--cleanup` flag, not default), because
  the user usually wants to inspect or keep the branches after a run.

## Alternatives considered

### Single working tree, sequential branch checkout per worker

**Rejected.** This serializes all workers on a global `git checkout`
lock, turning a parallel strategy back into a sequential one.

### Single working tree, per-worker subdirectories

**Rejected.** Does not solve cross-worker visibility for files that
live outside the assigned subdirectory, which is common for
dependency-graph edits.

### One clone per worker

**Rejected.** Full clones cost N × repo size with no object sharing,
and a clone loses the connection to the user's working branch.
Worktrees give the same isolation at a fraction of the disk cost.

## Validation

- `tests/test_fleet.py` covers both the worktree path and the
  temp-dir fallback.
- Integration merge logic reuses the same primitives the PCIV
  strategy uses, so the behavior is exercised by both strategies'
  test suites.
