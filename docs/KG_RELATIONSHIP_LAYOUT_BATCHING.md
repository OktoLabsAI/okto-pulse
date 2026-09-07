# Operation-local relationship layout resolution — 2026-09-07

The live KG Refresh profile after native COUNT and Health metric batching still
showed repeated board-route acquisitions while translating each physical edge
layout. In a 30-second stack-only profile (1306 samples, zero errors), 18 graph
stacks involved layout resolution; census spent 17 of its 109 classified stacks
outside the query batch. These are samples, not additive timing percentages.
Native COUNT was present in the census stacks: it was not accidentally bypassed.
Health still dominated (764 samples); this change does not claim to solve that
separate cost. Graph/stats returned HTTP 200 in 4.880/6.614 s during this profile.

## Change and authority boundary

CommunityRoutedCypherExecutor.relationship_table_names resolves an operation's
manifest using one freshly acquired route inside the existing operation window.
It only translates pure provider metadata; no query runs here. No names, route
authority, descriptors or data are cached across calls, boards or generations.
Actual query execution still independently acquires the route/window and Grafx
snapshot. A revoked/missing binding after mapping still refuses the query.

The graph page submits only layouts eligible for that page, retaining untyped
ID handling and code-traceability visibility. Census submits the 69 unique exact
layouts. Complete batch results replace scalar name lookups. Batch exception,
missing elements or malformed names discard the entire batch; existing scalar
resolution and per-layout diagnostics run unchanged. Failure never substitutes
logical names for an unavailable Grafx route. Providers without the optional
method retain scalar compatibility. Core and native storage protocols unchanged.

## Evidence

Read-only manifest comparison on the live board: 69 names, identical ordered hash
`157fae71bf13ab5ba33b114bffdf9feae2491e3af06fd776f467db844801a9e9`.
Scalar/batch/batch/scalar: 0.324 / 0.00888 / 0.01038 / 0.331 s.
The deterministic reduction is 69 route acquisitions to 1 for this mapping step.
Approximately 0.32 s saved here is not an end-to-end UI improvement of 30x and
does not explain away the earlier 21.53-second full censo observation.

Focused combined regression: 89 passed in 15.10 s across
test_relationship_layout_batching, test_kg_routes_grafx_relationship_layout,
test_routed_board_graph_facades and test_kg_read_phase_timings. Ruff passed.
Coverage includes provider switches, route loss between mapping and execution,
window release, empty groups, malformed/incomplete results, legacy executors,
per-layout refusal, identical census queries and page visibility filtering.

No spec consolidation, backfill, rebuild, redrive or reset performed. This source
milestone is separate from a global wheel release. Runtime deployment is tracked
in Grafx EVOLUTION_PLAN_CODEX.md.
