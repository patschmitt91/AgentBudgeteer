# bench

Ten fixture tasks that exercise different regions of the routing policy.
Each task lists the expected strategy for v0 so the decision tree can be
regression-tested without running real model calls.

The runner (`bench/runner.py`) will be built in a follow-up. For v0 the
YAML files document the expected routing for each task.
