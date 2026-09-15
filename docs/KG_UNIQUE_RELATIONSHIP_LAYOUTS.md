# Count each declared relationship layout once

The Core schema declarations contain `supersedes(Decision, Decision)` in both
`REL_TYPES` and `MULTI_REL_TYPES`: 70 declarations, 69 unique layouts. The
Community `_relation_pairs` helper previously concatenated them. The graph
response deduplicated emitted edges later, but census and verification totals
could count the same table twice and every consumer issued redundant work.

The helper now deduplicates exact `(logical type, source type, target type)`
triples in first-encounter order. Distinct endpoint layouts, directions and
logical types remain separate; actual parallel edge rows are not deduplicated
by this change. It applies to graph loading, edge census and the verification
route, independently of backend. No Core schema or Grafx-specific policy changes.

Focused suite: 55 tests passed in 11.33 s, covering helper order/identity, the
real duplicated declaration, both batched and non-batched census, physical
layouts, partial-failure diagnostics, phase timing and REST authorization.
Ruff passed. On the current real board, the earlier 70-statement census total
4425 included one `supersedes` edge twice; the native 69-table total is 4424.
This explains the one-edge difference without implying a graph mutation.

This is a correctness fix and removes one redundant query, not a claim that
the cold Knowledge Graph performance issue is solved. No recovery, redrive,
backfill or cognitive consolidation is triggered by this change.

## Runtime verification

Pulse 0.3.3 PID 7896 loaded this correction and Grafx `eaa7c65` (single read-only
adoption) from source after PID 15796 shut down with zero graph-close failures.
The first isolated stats request returned HTTP 200 in 6.261 s: 2779 nodes,
4424 edges, 69 layouts, zero failed layouts, logical `supersedes` total 33.

Browser KG loading preserved 500 nodes / 703 edges, 69 layouts considered,
64 scanned / 5 skipped and zero failed. Pagination reached 1000 nodes with
500 unique page IDs, 897 edges and zero failures. Total remained 2779.
The UI workload still took 24.758 s for graph and 24.555 s for stats after that
isolated read had warmed the instance; this is **not** a cold A/B or closure of
the remaining performance issue. Relation phases dominated both requests.

Cognitive counts remained 21 pending / 0 in progress / 19 consolidated.
