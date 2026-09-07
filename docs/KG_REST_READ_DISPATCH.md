# KG REST synchronous read dispatch

## Scope

The Community handlers `list_nodes`, `get_node_detail`, `find_similar`,
`get_supersedence`, `find_contradictions` and `cypher_query` now use the existing
backend-neutral Core `run_blocking_graph_io` bridge. Subgraph and census already
used this bridge. Authorization and Code Traceability visibility decisions stay
on the request path before native dispatch. The synchronous closures do not use
the request's relational Unit of Work.

Node page plus its total hint execute sequentially inside one dispatched task.
This does not introduce a shared graph snapshot across those two existing calls:
their previous visibility semantics are unchanged. Filters, query limits,
timeouts, result envelopes and structured provider errors are preserved.

The bridge copies request context and drains the native call on cancellation,
including repeated cancellation. A disconnected client must not free or replace
a graph participant underneath its still-running operation. This is deliberately
not immediate native-query abortion. Native operation budgets still apply.

## Expected benefit and limits

Removing these synchronous calls from the ASGI loop permits other requests and
worker coordination to run during graph I/O. It is not a claim of parallel Python
CPU execution, lower query complexity, or shorter physical commits. Independent
Grafx participants remain selected by the Community adapter; dispatch does not
create one participant per request or alter pool budgets. No authority check,
OCC pass, WAL flush, snapshot guarantee or multi-reader/writer capability is
removed. No extra retry or result cache is introduced.

## Evidence

The exact staged Community source, with unrelated settings/DLQ/UI changes excluded,
passed 62 tests in 37.31 seconds across read dispatch, Code Traceability REST
authorization/refusal mapping, power authorization, graph phase timing and native
relationship-layout resolution. The new six-route test checks off-loop thread
identity, context propagation, arguments and results, and runs an event-loop
callback while native work is pending. A separate repeated-cancellation test
checks that native cleanup precedes propagation to the caller. Existing policy
tests cover denied access rather than relying on the dispatch test's policy mocks.
Ruff and staged whitespace checks passed.

The unchanged Core bridge and its Global worker integration also passed their
13 focused tests in 7.55 seconds against the same isolated source combination.

These handler changes were already in the source used to launch Pulse PID 18920;
recording this milestone does not require another restart. This milestone adds
regression coverage and traceability, not a new live speedup comparison. No
production repair, consolidation, redrive or benchmark spec was consumed.
