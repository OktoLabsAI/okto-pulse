# Grafx related-context filters

The Grafx adapter and routed board facade now expose the optional
`find_by_artifact_filtered` capability already consumed by Core
`KGService.get_related_context`. Previously the facade lacked this method and
the service silently used its unfiltered two-hop fallback for requests carrying
direction, relation-type or depth options. This was a compatibility defect,
not a reason to change the native graph protocol or add Grafx imports to Core.

## Contract

- `direction="incoming" | "outgoing" | "both"` restricts the first hop only.
- `rel_types` accepts a list of known logical relationship names. Duplicated
  names do not duplicate queries; an empty list or `None` means unrestricted.
  Unknown names, physical table names and malformed inputs are refused before
  graph I/O, never interpreted as a request for all relations.
- `max_depth=1` performs no second-hop reads and returns null hop2 fields.
  `max_depth=2` retains the undirected second-hop neighbourhood, even when hop1
  types or direction were restricted.
- Expanded neighbours retain the existing graph-layer, superseded, active-memory
  tombstone and Code Traceability visibility rules. The explicitly requested
  anchor can be from another graph layer, as in the existing Core query contract.
  A missing/invisible hop2 keeps the optional null extension.
- Parallel relationship occurrences and existing layout iteration order remain
  intact. Filtered and unfiltered adjacency memo entries cannot contaminate one
  another; the cache key includes first-hop type/direction scope.
- The routed method enters the ordinary operation window and selects only the
  persisted provider. Missing capability is a typed refusal, not unfiltered
  success or a fallback to another backend.

No filters changes the legacy service route and result shape. The native
single-hop queries already support the required operations; schema-specific
layout selection belongs in Community. No Core implementation changed.

## Result limit correction

`max_rows` limits expanded result rows, not candidate anchors. The previous
`LIMIT $max_rows` on the center-only query could stop at isolated/invisible
centers and omit later valid neighbours. The center lookup now evaluates all
matching anchors under the same transaction and existing native statement
budgets; expansion still stops at the requested result limit. A very large
anchor set may reach those native budgets rather than return a silently
truncated answer. This correction is not claimed to reduce center lookup cost.

First-hop filters reduce the selected layout/direction queries, and depth 1
avoids the entire second-hop expansion. This is work legitimately excluded by
the caller, not speculative skipping of corruption checks for selected queries.
General unfiltered layout fan-out remains a separate residual.

## Validation — 2026-09-07

- First filter/facade slice: 75 passed in 37.32 s.
- Final real-Grafx store/filter integration: 53 passed in 56.48 s, including
  Core service → routed facade → actual Grafx reads.
- Existing Core related-context contract: 8 passed in 2.41 s, in its own test
  process. These counts overlap and are not a distinct-case aggregate.
- Ruff passed. Real engine tests use newly created temporary databases; no
  cognitive spec or live board data is mutated.

Coverage includes directions, types applying only to hop1, optional hop2,
duplicate edges, null depth-1 fields, no second-hop execution at depth 1,
layer/superseded/tombstone policy, CT exclusion at both anchor and neighbour,
invalid input refusal before I/O, operation-window forwarding, missing provider
capability and `max_rows=1` with an isolated first center.

Native source under test includes scalar-PK optimization `9420b49`. A subsequent
controlled restart on 2026-09-07 loaded the filter correction into Pulse PID 18604:
startup verified the Community source path and availability of the filtered
method on both store and routed facade. The live KG read returned HTTP 200 with
500 nodes, 703 edges and zero failed edge tables; UI pagination reached 1000 nodes.
This validates deployment and ordinary graph reads, not all filter combinations
over live MCP: that contract is covered by the real-engine integration tests
above. No cognitive spec was consumed or package published.
