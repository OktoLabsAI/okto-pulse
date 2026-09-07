# KG census request isolation — 2026-09-07

The live browser audit found two overlapping `stats?graph_layer=canonical`
requests on opening Knowledge Graph. `KnowledgeGraphPage.loadDiagnostics`
depended on health/historical permission hydration and unnecessarily repeated
the census when those permissions became ready. The census was also published
only after the slower Health request completed.

The census now has its own callback, request sequence and lifecycle keyed to
board and graph layer. Permission hydration does not repeat it. Explicit refresh
still refreshes all three diagnostics and the graph; this is not a persistent
cache or a change to backend authorization. Board/layer changes and unmount
invalidate outstanding census answers. No Grafx-specific code enters Core.

Health and historical completion publish independently. An empty graph waits for
its initial historical status before mounting onboarding, whose completion
callback can otherwise trigger repeated refreshes while Health is delayed.
Non-empty graphs do not wait for diagnostics. An unmounted onboarding component
also ignores its outstanding initial progress response. This changes UI lifecycle
only, not backfill jobs, queue state or cognitive consolidation.

The same checkpoint includes the previously pending graph-layer census total:
the controls use available type counts instead of unavailable Health metrics,
retaining the existing Code Traceability authority gate.

## Evidence and scope

- Focused diagnostics and controls suite: 26 passed in 4.24 seconds. Cases cover
  permission hydration, explicit refresh, stale previous-board responses, delayed
  Health with completed historical progress, unmounted onboarding callbacks,
  non-blocking populated graphs, census totals and permission loss.
- An initial slow-Health fixture exposed the onboarding refresh interaction; it
  was corrected before acceptance, not removed from the final empty-graph test.
- Before this frontend correction, native Grafx scalar-PK `9420b49` and Community
  filtered-context `440070b` were loaded into Pulse PID 18604 from source.
  Opening the KG after restart measured graph 42.464 s and overlapping census
  requests 49.429/57.001 s. Warm cursor continuation 500 → 1000 nodes took 1.945 s.
  A separate warm REST read returned 500 nodes, 703 edges, zero failed edge tables
  in 1.486 s. The UI showed total 2779 canonical nodes.
- These observations are not a controlled cold/warm A/B or a claim that duplicate
  census explains the entire cold latency. Cold admission and unfiltered layout
  fan-out remain separate residuals of the existing performance plan.
- No reserved spec was consumed: 21 pending, 0 in progress, 19 consolidated;
  cognitive ledger SHA256 remains
  `4AFF1AB6EE6C6E621C6598148154A04298500B8DA92EBF0F5E90AD081B2217F4`.

No PyPI publication, main merge, graph reset or DLQ redrive is part of this
checkpoint. Packaged frontend assets are built from the current local Community
worktree, which still contains other integration work; this is not a clean release
artifact claim.

## Live frontend verification

Controlled deployment after the correction: Pulse PID 26560, API 8100 and MCP
8101, default data home made explicit through `DATA_DIR`. Previous PID 18604
reported global close and one board close with zero errors (75 ms). The browser
loaded `/assets/index-CIG6q9cO.js`; TypeScript and production build passed, with
packaged tree SHA256
`7435cd528f0755affd64004946496a6db35fe315fc48bffcef1ce5bd85bb770d`.

The new browser navigation recorded exactly **one** census request despite
permission hydration. Graph 500: 45.340 s; census: 53.187 s. Thus the duplicate is
removed, but the cold latency is **not solved**, nor improved in this observation.
No causal percentage speedup is claimed. Clicking `Load more (500+)` then returned
500 unique page nodes and 897 edges in 2.292 s, HTTP 200, 66 edge tables scanned,
4 skipped by page type, zero failed. The UI reached 1000 nodes, total 2779.
Final cognitive read remained pending 21 / in progress 0 / consolidated 19.
