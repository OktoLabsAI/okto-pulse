# Targeted deterministic projection repair

`POST /api/v1/kg/boards/{board_id}/deterministic-projection/repair`

This operational recovery endpoint fills a missing deterministic source projection
without launching a board rebuild or cognitive consolidation. It uses the existing
deterministic queue/worker, authoritative source loader, write admission, claim
fences, graph transaction and normal delivery/audit paths. It is backend-neutral.

Example request:

```json
{"spec_ids":["cbd4eedd-a16b-51ae-93b6-5f850467fb4d"],"reason":"Repair verified missing deterministic provenance root"}
```

- Requires authenticated board editor/admin access and queue-reprocess authority.
- Requires `graph_state=healthy` and an explicit `overall_state` of `healthy` or
  `at_risk`. The latter permits missing projections with historical pending work;
  it does not bypass worker authority. Missing, malformed, unknown, recovery,
  quarantine and backpressure states fail admission closed. Worker admission
  still independently checks current authority before any graph mutation.
- Explicit 1–25 UUID spec IDs; no `all`, recursive or implicit board-wide scope.
  Every selected source must belong to the board, be unarchived and have status
  `done`, `approved` or `validated`. All sources are validated before enqueue.
- A 3–1000 character audit reason is required. Queue admission and the request
  audit are committed atomically, then the normal worker is signaled. The audit
  correlation ID is returned and included in queue payloads. Scheduling is not
  misrepresented as a graph commit (`committed_at` remains unset on this receipt).
- HTTP 202 means admission, **not completion**. `queued_spec_ids` identifies actual
  queue writes; `coalesced_or_fenced_spec_ids` identifies suppressed admission
  (active/paused/rebuild work or deletion fence), not success. Inspect queue/DLQ
  and graph provenance readback to confirm completion.
- Repair does not revoke an existing claim, resume a paused job, take over exact
  rebuild membership, delete tombstones, reset a graph generation, fabricate root
  nodes, or edit any source. Normal semantic events retain their existing queue
  invalidation behavior; only repair requests opt into active-work coalescing.
- Cognitive pending/consolidated states are not changed by this endpoint. The
  caller can preserve pending specs for later cognitive/performance benchmarks.

Use after verifying a source exists but its deterministic projection is absent.
Do not use as a substitute for quarantine recovery, to bypass an active rebuild,
or as a way to mark cognitive work complete. Unknown/unavailable health and source
eligibility failures must be investigated, not replaced with fabricated graph data.

The September 7 audit identified seven such missing roots with no queue/debt rows;
the endpoint was implemented and tested on isolated data, then admitted exactly
those seven sources through authenticated REST (correlation
`665ff221-7d3f-4a76-8d76-a3401daa7db2`). All seven obtained commit receipts and
canonical-root readback. The cognitive ledger remained byte-identical, with
21 pending specs reserved for benchmarks. Typed missing-prerequisite waits now
yield to other work in the same board rather than starving their own prerequisites;
ordinary error/backoff, active claims and exact rebuild barriers remain intact.

The downstream Global batch also completed on 2026-09-07 at 14:09:24 UTC: all
seven remaining events received their normal post-verification ACK, including
the original cognitive delivery previously blocked by these missing roots.
Active consolidation and Global queue depths are zero. REST reconfirmed the
same 21 pending cognitive benchmarks and byte-identical ledger. Historical
unrelated DLQ/debt remains visible; this repair does not claim to clear it.
