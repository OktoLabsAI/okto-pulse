# Scoped reader-lane selection — 2026-09-07

Status: implemented with its independent-reader dependencies in a bounded
Community source milestone. The exact staged candidate was exported and tested
independently of the other worktree changes. This is not a published pip release.

## Evidence and scope

A live 25-second, 30 Hz profile including idle stacks captured 16200 thread
samples, zero errors. Of 105 census stacks, 56 were entering the Grafx participant
section and 43 ended in its OS-lock wait sleep. Graph/stats returned HTTP 200 in
3.959/5.370 s. These are stack counts, not additive CPU or wall-time percentages.
Health had both CPU work and participant waits; native cold admission alone is
not a sufficient explanation for all UI latency.

Uninstrumented fresh native read-only admission measured 0.928/0.949 s on this
board. Two concurrent thread opens measured 1.277/1.568 s each; a two-process
group including startup cost 2.006 s versus 1.648 s for threads. All six schema
read results matched. This small diagnostic does not justify moving graph reads
to processes. cProfile's earlier 2.192/2.247 s opens include substantial profiler
overhead and must not be substituted for ordinary runtime latency.

## Scheduling change

`GrafxReadLanes.reserve(board_id)` tracks current scoped operations per existing
read lane. Choose the least occupied lane, with round-robin tie breaking. Counts
cover admission/open, the full query/batch/paired snapshot, result shaping and
transaction cleanup. Reservation releases on success, error, timeout and base
exception, and empty board state is removed. The lock protects only bookkeeping,
never graph I/O. No new worker, wait queue, handle, retained route or authority
cache is introduced; production still has two lazy read-only pools.

Example addressed: A holds lane 0, B uses and releases lane 1, then C arrives.
Blind round-robin sends C behind A; load-aware selection uses idle lane 1. If both
lanes are occupied, choose the less occupied one and let the original native
participant/lease protocol decide admission. This does not eliminate overload or
promise fairness against unscoped callers. Counts are scheduling hints, not locks,
snapshots, permissions, connection leases or evidence of native idleness.

Community Cypher scalar/pair/batch and GraphStore `_read` use the scope. Legacy
constructors retain their scalar resolvers. Metadata/vector callers still using
an unscoped resolver are not counted; safety is unaffected because every call
still goes through route admission and native transaction checks. Writers,
close/recovery fencing, WAL and both OCC checks are unchanged. Core has no new
Grafx-specific API. Read-join checkpoint/retry remains the existing guarded path.

## Tests and remaining work

84 related tests passed in 14.52 s across lane scheduling, Cypher, reader retry
and routed composition. Two added boundary cases brought the focused lane suite
to 22 passed in 7.00 s (overlapping the earlier run): actual composition scope
delegation/refused resolution and KeyboardInterrupt cleanup. Ruff passed.

The combined independent-reader candidate passed 294 tests in 51.26 s from an
isolated export of the Git index: pools/budgets, executor/store, routing,
lifecycle/recovery, relationship translation and scheduling. This export excludes
the unrelated UI, deterministic repair and Global write/index changes. Checkpoint
refusal logging also omits raw exception messages and restricts metadata fields
to bounded ASCII identifiers; this does not change recovery decisions.
After that final log-only adjustment, the exported operational-provider suite
passed 27 tests in 7.82 s (overlapping the combined suite). Ruff passed for all
Python files in the candidate.

No claim that the cold-open O(N) watermark verification is removed. `OPEN-1`'s
existing rejection of a header-only/circular completeness proof remains valid;
the heap history is still checked on every new participant. No benchmark spec
was consolidated and the pending ledger hash is unchanged. Live source deployment
observations are recorded in Grafx EVOLUTION_PLAN_CODEX.md.

## Live source validation

Pulse 0.3.3 PID 35808 loaded the scoped selection from the worktree. First Refresh
after reported API readiness: graph HTTP 200 in 14.704 s, 500 nodes / 703 edges;
census HTTP 200 in 14.348 s, 2779 nodes / 4424 edges, zero failed tables. Phases:
schema 10.924 s, node sample 0.848 s, node counts 0.601 s, edge counts 1.876 s.
Graph node/edge phases were 7.380/7.101 s. The prior source runtime's 41.543/28.770 s
observation is not a controlled A/B ratio; cold schema admission remains slow.

Pagination added 500 unique node IDs and 897 edges in 1.258 s, zero failed tables,
UI 1000 / total 2779. The pending-spec ledger remained byte-identical. No global
wheel was rebuilt and no package was published. The source milestone includes
the independent-reader dependencies. The later checkpoint-log sanitization is
source-tested but was not a reason to restart this live process.
