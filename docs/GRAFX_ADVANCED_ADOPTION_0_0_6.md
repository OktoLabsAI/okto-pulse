# Grafx 0.0.6 advanced adoption in Pulse

Authorized September 13, 2026. Grafx checkpoint `6db72269317f6c86081d48152e86d211d732c17c`
is committed and pushed on `feature/v0.0.6`. This work uses the paired Community
and Core worktrees already qualified with that engine; it is not a global install
or authorization to mutate production data.

## Fixed implementation order and acceptance

| Order | Capability | Delivery boundary | Status |
| --- | --- | --- | --- |
| 1 | Native composed neighborhood reads and bounded supersedence frontiers | Preserve physical/logical identity, bag multiplicity, current filters, first-hop-only options, null extension, result-prefix ordering, snapshot and explicit errors; reduce repeated adapter query calls without enumerating all graph trails. | Implemented; source, HTTP and private installed qualification passed |
| 2 | Native text retrieval and hybrid candidate retrieval | Optional neutral ranked-search contract; explicit index support/readiness; filters before top-k; preserve substring semantics and the existing domain expansion/ranking pipeline. No silent empty result on failure. | Implemented and qualified; source/HTTP/private installed tests passed |
| 3 | Independent Global Discovery readers | Bounded participants and short admission; preserve writer fencing, generation validation, operation lifetime and exclusive drain for restore/promotion/shutdown. Do not bypass Core's same-board saga policy. | Implemented and qualified; native overlap, lifecycle/fence and Settings tests passed |
| 4 | Commit provenance and retained history/diff | Explicit opt-in, neutral observations for application audit, bounded retention and coordinated format activation; never imply atomic SQLite/graph restore or fabricate pre-activation history. | Implemented and qualified; native history, Core guarded lifecycle and HTTP permissions passed |
| 5 | Bounded graph analytics | Optional neutral diagnostic results for cycles/components/dependency impact; explicit scope, costs and incomplete/unavailable outcomes. Do not capture the full graph for every KG page. | Implemented and qualified; native algorithms, filtering/bounds and HTTP tests passed |

The Core owns business semantics, authorization and provider-independent ports.
All native Grafx types, indexes, handles, identifiers, activation and errors stay
in Community. Other adapters may explicitly decline optional capabilities.

Each increment requires focused real-engine and boundary tests. Batch broader
affected regression and isolated installed API/UI/MCP checks at integration
checkpoints, not after each small edit. Preserve failed receipts when diagnosis
changes. No percentage performance gates, production backfill or reserved-spec
consumption; report query/commit counts separately from measured latency.

## Increment 1 design

Adjacent reads currently query each physical relationship/direction separately.
Compose those same branches with native UNION ALL in bounded groups, retaining
branch identity/order and every duplicate. Keep the existing caller's selected
frontier and visibility checks: a full result prefix must not hide a late failure
inside an already-selected adjacency. Native read-query budget refusals may split
an unpublished batch in the same read transaction; a one-branch refusal propagates.
Other errors, cancellation and lease loss must not trigger splitting.

Supersedence remains breadth-first over unique nodes rather than switching to
all-trail enumeration, which could multiply work on diamonds/cycles. Batch the
current frontier through indexed UNWIND map keys and retain the existing visited
set, depth semantics and active-node filters.

## Increment 1 contracts and consumption

Implemented in Community's `grafx_composed_reads.py` and `grafx_graph_store.py`.
Core source, public graph-store signatures, REST/MCP request/response schemas,
permission rules and write fences are unchanged. No new Core dependency on Grafx
or migration of existing graph data is introduced.

Existing `find_by_artifact` / `find_by_artifact_filtered` callers (including
`KGService.get_related_context`) automatically use grouped native adjacency reads.
The same projection, physical endpoint identities and logical relationship names
remain authoritative. UNION ALL preserves parallel edges and the two directional
appearances of a self-loop; IDs shared by different node types remain distinct.
First-hop direction/type filters do not silently restrict a second hop.

`traverse_supersedence(board_id, decision_id, max_depth=10, node_type="Decision")`
retains breadth-first ordering, a unique visited-node set and the depth boundary.
Its active-read predicate excludes permanent source/projection tombstones; it
intentionally DOES NOT exclude superseded decisions, which are part of history.
The Core service and the existing
`GET /api/v1/kg/boards/{board_id}/supersedence/{decision_id}` endpoint retain their
current chain-selection and serialization semantics. This is not a change to the
main KG node-page or Global Discovery search implementation.

No new Settings option is needed. Internal upper batch sizes are 16 relationship
branches and 128 frontier inputs. Native configured row budgets still apply to
every execution. If a grouped read alone exceeds a native query row-admission
budget, it is decomposed in the SAME read transaction before returning that
attempt's rows; it never opens a fresher snapshot halfway through the operation.
If one original query still exceeds its budget, the refusal is propagated.
Low limits can therefore cost additional attempts, not silently truncate results.
Malformed rows/branch ordinals are rejected before yielding that attempt's rows.
Cancellation, deadline, corruption, unsupported operations and lease errors do
not trigger batch splitting. The caller's existing bounded error/recovery policy
is unchanged. This mechanism must not be reused to retry writes.

## Increment 1 validation checkpoint — September 13, 2026

Source qualification against the committed Grafx 0.0.6:

| Check | Result |
| --- | --- |
| New native/operation/HTTP tests | 68 passed, zero failures/errors/skips |
| Affected Community regression | 198 passed, zero failures/errors/skips |
| Additional neutral Core history tests | 12 passed, zero failures/errors/skips |
| Private installed wheels: native operations, HTTP, filters, lanes and authorization | 155 passed, zero failures/errors/skips (overlaps the source checks) |
| Ruff on changed production modules and new tests | Passed |

The 278 source tests above are disjoint selections, not a claim that the entire
Pulse test suite was run. The new tests use real native storage (pure and NumPy
for UNION result semantics), an independent old-algorithm oracle, duplicate
edges/self-loops, type-qualified identities, cyclic and diamond BFS, filters and
null-extension behavior. Low-row-budget tests commit through an independent
writer while a pinned read splits, proving that both returned observations retain
the original snapshot. Bounds are also tested beyond one 16/128-input batch.
Errors and invalid native outputs are injected without converting them to empty
success. A real HTTP TestClient call traverses the actual Core service, routed
store and Grafx; its authorization dependency is controlled in that test, with
separate denial/dispatch/ownership suites in the 198-test regression.

Structural evidence: an unrestricted Decision adjacency uses **1 native execute**
for its 16 physical/directional branches. An already-single-branch filtered read
still uses 1. Frontier queries scale with bounded selected BFS batches rather than
one execute per visited input. This does not imply fewer edge evaluations or a
16x latency improvement. The latest source fixture's warm composed-adjacency
median was **11.169 ms**, six alternating observations inside one read snapshot
after warming both implementations. That small synthetic observation is not an
end-to-end UI, throughput, or growing-graph benchmark and is not a release gate.

### Preserved failed observations / limitations

- The first operation test run passed the old-algorithm equality assertions but
  five additional assertions incorrectly expected superseded decisions to be
  removed from history. The existing Core `active_read_filter_clause` explicitly
  excludes permanent tombstones, not `superseded_by`. Only those test expectations
  were corrected; native and domain semantics were not relaxed.
- Two extra old Core modules (`test_kg_supersedence_chain_generic.py` and
  `test_kg_recall_superseded_filter.py`) failed collection because their
  `kg_schema_testing` imported removed Ladybug adapters. On September 13 the user
  explicitly requested their removal: both modules were deleted, not skipped or
  counted as passed. Their useful storage-independent checks now live in
  `test_kg_read_port_contracts.py`, with neutral fake ports and no native imports.
  Current Community native tests cover traversal/recall integration. The old
  failure receipt remains historical evidence, not an outstanding requirement to
  restore Ladybug. Other historical test helpers were not swept or restored.
- The first private installed attempt lacked the Windows `pywin32` package and
  failed collection (`pywintypes`). The package was supplied in the private
  environment only; application code and test assertions are unchanged. Installing
  the existing qualified version 312 resolved collection; all 155 checks then passed.

Receipts live under the Grafx checkout's `.grafx-tmp/`:
`pulse-v006-composed-real-qualified.xml`, `pulse-v006-composed-regression.xml`,
`pulse-v006-composed-core-history.xml`. The failed observations are preserved as
`pulse-v006-composed-real-operations.xml`,
`pulse-v006-composed-core-regression.xml` and
`pulse-v006-composed-installed/installed.xml`.

Private installed evidence: `pulse-v006-composed-installed/installed-qualified.xml`
and `pulse-v006-composed-installed/proof-qualified.json`. The interpreter ran in
isolated mode, with no checkout package paths or source conftest, using existing
user-site third-party dependencies behind the private package location. All 251
Grafx, 383 Community and 825 Core package payload files matched source/wheel/private
installation before and after the run; selected test input hashes were unchanged.
This is an installed-package qualification, not a globally installed Pulse update.

| Qualified wheel | SHA-256 |
| --- | --- |
| Grafx 0.0.6 | `d666704a14492d737c43f0293e71605aeaf279aad65b4086d08bcb0160020b70` |
| Community 0.3.3, increment 1 | `05241740679a94416fdf681bc82c866fdd14b440874c48e4d2e568a2df563c7f` |
| Unchanged paired Core 0.3.3 | `54c1c2e81b2170fde44bd0bb0efb06c2151092125a08c90d43b118cc5425c43b` |

Qualified receipt SHA-256:

- Native/operation/HTTP source: `3c83ef715146f4f2c5b346d5b116d0e8c9836c374191a4561f81b7a2f85a54a4`.
- Community regression: `96b81a0a1b31874b25644b3fc82599d8eb71931146d78c709aed99dc25ca62da`.
- Core history: `bf7ee6538fc1719a891427c18f5e9c73f21cb979c987e64513621e34b2476680`.
- Installed tests: `a63289b671b2c469d3983d2dcdee66d9f6446467d16e70f0f6d9d376cdca845f`.
- Installed identity/input proof: `dca5803ea6f33500d2199cf321c9bbad8d4f3d856023d54fe59d256af095b3fb`.

No global Pulse installation/restart, production read/write, source consolidation,
queue redrive, browser validation of a new global build or full UI latency claim
is part of that increment-1 checkpoint. The following sections describe the
subsequent implementation of increments 2–5, not part of those earlier receipts.

## Increments 2–5: architecture and API contract

The optional Core registry ports are `ranked_graph_search: RankedGraphSearch`,
`graph_history: GraphHistory` and `graph_analytics: GraphAnalytics`. Their default
is `None`; they are not mandatory bootstrap slots. Another graph adapter may
decline any of them, yielding a neutral capability-unavailable response (HTTP 503),
not an empty successful result. Core imports no Grafx implementation or value.

Community wires the providers in the existing routed board composition. All
observations own a board operation pin and validate the same physical route on
completion. Index/history preparation and retention use the exclusive schema
mutation window, current Core writer revalidation, and close/reopen all board
participants. They never bypass saga policy or overwrite route bindings.

REST prefix: `/api/v1/kg/boards/{board_id}/exploration`. Blocking native work and
query embedding run outside the event loop via the existing cancellation-draining
graph-I/O boundary. Existing board access is checked before capability dispatch.
Reads require `kg.query.related_context` (legacy `board:read`). Native failures
retain neutral error mapping, not HTTP 200 with empty data; invalid requests
are 400/422. Public index preparation, history activation and history pruning
were retired under Pulse v1.3 F4. Their former routes return 404 before
authorization or storage dispatch. Historical reads retain their additional
audit and Code Traceability permissions. Internal adapter capabilities do not
grant public write authority or cause reads to prepare missing storage.

| Method / suffix | Input | Result / effect |
| --- | --- | --- |
| GET `search/readiness/{node_type}` | Supported Pulse node type | `supported`, `ready`, available `modes`; never builds an index. |
| POST `search` | `SearchRequest` below | Ranked `hits` with qualified business `node_type`/`node_id`, title, independent lexical/vector scores, ranking regime, snapshot and `complete`. |
| GET `history/commits` | `after` opaque token optional, `limit=100` (1–1000) | Ascending verified commits, metadata, observed/ordered time, tracked-after boundary, snapshot, `has_more`, `next_after`. |
| POST `history/as-of` | scope, `at` opaque commit token; bounds below | Historical nodes and edges with business IDs, properties and qualified opaque row lineages. |
| POST `history/diff` | same as as-of plus `before` token; `at` is the after coordinate | Native row/schema changes, before/after business entities, label/property changes and complete bounded result. |
| POST `analytics` | selected node/relationship types and algorithm/options below | Bounded induced-graph components, cycles or dependency impact; explicit scope and snapshot. |

### Text and hybrid retrieval

`SearchRequest`: `node_type`, nonblank `query` (maximum 4096 characters),
`mode=text|hybrid` (text default), `limit=20` (1–200),
`candidate_limit=100` (1–1000 and at least limit),
`max_filter_rows=10000` (1–100000), `timeout_seconds=10` (0.001–30),
`graph_layer=canonical|working|all` (canonical default),
`min_confidence=0.5` (0–1), `include_superseded=false`,
`include_code_traceability=false`, `phrase=false` (text mode only).
JSON requests reject unknown fields and non-finite numeric options.

The adapter creates `pulse_text_v1_<node_type>` over title/content, with weights
2:1 and native durable BM25 statistics. Analyzer/default native document/token
admission bounds also apply to indexed writes: opting in adds write amplification
and can refuse documents outside those bounds. Preparation is not a performance
toggle safe to enable blindly for an existing corpus. Its reason is returned and
logged; this is an operational log, not an atomic SQLite audit receipt.

Text mode uses native lexical retrieval; it does not replace substring matching.
Hybrid mode asks the configured embedding provider for the existing 384-dimensional
space and uses native lexical/vector candidate fusion. RRF is not cosine similarity
or a probability. Raw source scores and exact/approximate regimes are retained;
complete means both requested sources completed, **not** globally exact hybrid
top-k. Candidate bounds and HNSW approximation still apply. The existing Core
domain expansion/recency/confidence/reranking pipeline remains unchanged.

Visibility is applied before top-k in the same read snapshot: permanent tombstones,
superseded policy, graph layer, confidence and Code Traceability. The current
RecordIdFilter requires a **bounded O(N) scan of selected policy columns**; hitting
the cap refuses, it never truncates the allowed set. This is an opt-in retrieval
capability, not a claim of sublinear end-to-end filtered search. Native corpus
statistics include all indexed documents, so HTTP ranked search additionally
requires the full Code Traceability KG read authority even when CT hits are off.
The REST caller cannot inject a vector or self-grant CT authority.
The retained visibility payload also has a fixed 16 MiB logical admission cap
(conservative string-width accounting, not an RSS ceiling); oversized titles or
references cannot turn a row-count limit into an unbounded allocation.

Example (after explicit authorized preparation):

```json
{"node_type":"Decision","query":"durable graph recovery","mode":"hybrid","limit":20,"candidate_limit":100,"graph_layer":"canonical"}
```

### Independent Global Discovery participants

Normal Global operations retain shared lifetime pins; independent read sessions
do not wait behind the router's entire writer/verification scope. The existing
local writer ordering and Core writer lease remain. Each operation revalidates
its route before and after execution. Lifecycle/recovery/privacy/shutdown use
the **same** gate exclusively and drain outstanding pins; admission/drain is
bounded at 10 seconds and failure is explicit. Writer flush/close/reopen rotates
only its own handle; actual lifecycle closure retires every Global participant.

`kg_grafx_read_participants` now controls readers **per board and for Global**:
default 2, range 1–8, restart required, least-occupied lane preference. Settings
label/help and health budget projection reflect this. With R readers and B MiB
page budget per handle, configured page-cache capacity is `(1+R)*B` per resident
board **plus `(1+R)*B` for Global**. This is not an RSS bound: indexes/workspaces
and application data consume additional memory. No new startup toggle is needed.

Global read sessions use separate normal native participants so they can join
the current durable WAL without acquiring a Core writer lease just to checkpoint.
They are **not file-level `read_only=True` connections**. The session write fence
refuses every mutation; the routed read door and native transaction use read mode.
Board read-only participants are unchanged. Grafx's publication/OCC/WAL protocol,
not this scheduling gate, remains data authority.

### Provenance, historical reads and retention

The internal adapter activation capability is one-way and scoped to explicit Pulse node types and logical
relationship names. Only physical endpoint pairs wholly inside the selected node
scope are selected; requesting a relationship with no such pair is refused. All
three native activation phases are independently atomic/idempotent; the whole
sequence is **not** one transaction. An interrupted activation can be retried,
and may already have durably enabled earlier phases. Participant closure is
required; no old native client should join after the capability upgrade.

After opt-in, normal board graph-store mutations and graph transactions attach
`origin=okto-pulse.community`, the board ID and operation. Actor/correlation are
not guessed. Unactivated stores keep ordinary writes: only the exact native
pre-begin `enable_commit_history` refusal permits a metadata-free begin; failed
commits and other native failures are never retried this way. Maintenance,
transfer/rebuild candidate databases and Global writes are not claimed to carry
this board provenance. Raw native commits may legitimately have no metadata.

History starts with the activation baseline; it cannot recover earlier versions.
Commit tokens are opaque and store-qualified; foreign/future/pruned coordinates
are refused. Delete/recreate produces different lineages even for the same business
ID. All history HTTP operations require full CT authority and the existing
`kg.operations.audit.read` permission (legacy `kg.admin.settings_read`), because
they intentionally include historical/tombstoned/superseded values and commit
metadata. Activation and pruning have no public HTTP route after F4 retirement.
Denial of a retained read occurs before native access. Embedding payloads are
explicitly omitted; historical business IDs and relationship endpoints are retained.

As-of/diff bounds: `max_rows=1000` (1–10000), `max_bytes=16777216`
(1–67108864), native `max_events=100000`. Diff uses the native diff plus two
historical pictures to resolve business IDs/endpoints: the byte/event budget
applies to each bounded native call, not their sum or process RSS. All use one
reader snapshot. Separate HTTP commit-history pages observe fresh snapshots;
`next_after` is keyset continuation, not a retained server-side snapshot session.

The internal retention capability accepts a before token and byte cap (same default/range). It respects
native pins and reports logical redaction, not secure erasure or disk shrinkage.
Physical bytes reclaimed are normally zero. It never compacts online, schedules
background deletion, restores SQLite or runs backfill. This adapter description
does not expose an agent-callable retention operation or authorize data loss.
No cross-database atomic audit claim is made.

### Bounded diagnostics

Algorithms are `components` (weak connected components including isolates),
`cycles` (topological result plus strongly connected cyclic groups) and
`dependency_impact` (BFS including its source). Cycle `blocked` includes cycle
descendants and must not be mislabeled as only the cycle members.

Inputs: node types and logical relationships, `graph_layer=canonical`,
`include_code_traceability=false`, optional qualified `source_type`/`source_id`
(required for impact), `direction=out|in|both` (out default),
`max_depth=10` (0–100), `max_nodes=1000` (1–10000),
`max_edges=10000` (1–100000), `timeout_seconds=10` (0.001–30).
The native picture has a 64 MiB logical budget and 2,000,000 work units.
Node/edge capture and policy reads share one snapshot. Node tombstones and
superseded nodes, hidden CT nodes and wrong-layer edges/nodes are removed before
algorithms; hidden nodes cannot remain as path bridges. Unlayered relationship
tables are governed by their endpoint visibility. Parallel edges and self-loops
retain native semantics. Missing/hidden impact source is an explicit refusal.

This deliberately costs O(V+E) for the selected captured subgraph, with bounded
policy scans and output, and is **not invoked by ordinary KG page loads**.
Capture/admission/algorithm failures return errors, never a partial graph labeled
complete. Limits apply before filtering as well: a scope larger than its capture
cap is refused even if most nodes would be hidden. No persistent analytics cache
or mutation is introduced.
The additional business-ID/title payload has a separate fixed 16 MiB logical
admission cap; the native graph-picture budget does not cover that payload.

## Delivery qualification — increments 2–5

Focused source checkpoints: 21 initial native ranked-search tests; 37 combined
history/analytics/Global concurrency and routing tests; 38 HTTP/board/root
composition tests; 17 Settings frontend tests. These selections overlap later
regression and must not be added to it as distinct coverage. The 20 neutral Core
retirement/history checks also passed.

### Source and frontend qualification

| Check | Result / scope |
| --- | --- |
| Grouped affected Community regression | 1,092 passed; two stale test setups and one associated teardown error were corrected and requalified below. 1,094 tests selected; this is not the entire Pulse suite. |
| Corrected feature/integration selection | 68 passed, zero failures/errors/skips; real engine, HTTP, routed activation, search snapshot and Global concurrency. |
| Core optional ports, neutral reads/history and registry regression | 68 passed, zero failures/errors/skips. |
| Core writer lock, async mutation offload and write barrier regression | 56 passed, five pre-existing skips for edition-owned filesystem assertions; zero failures/errors. |
| Actual Community Global bootstrap/purge fencing | 2 passed: missing durable authority refuses before opening a native session. |
| Settings frontend | 17 passed; TypeScript/Vite production build and packaged asset synchronization passed. |
| Ruff on changed/new capability modules and tests | Passed. |

The corrected feature selection overlaps the grouped regression. Counts above
are per run, not a deduplicated grand total. The grouped run excluded the known
heavyweight schema-evolution/global-recovery-worker extensions; there is no claim
of a new complete native-engine regression, live browser exercise or global
installation. The native engine itself remains the already qualified commit
identified at the top of this document.

Adversarial coverage includes same-snapshot visibility while another native
participant commits, real reader progress inside Global writer verification,
exclusive drain/admission timeout cleanup, route/fence loss, read-session write
refusal, provenance fallback limited to pre-begin opt-in refusal, history
activation/reopen/diff/delete-recreate/retention, hidden-path filtering, algorithm
and payload budgets, REST authorization, denied administration and actual Core
guarded-write durability through the routed native lifecycle. Tests use private
stores and controlled authentication/coordination doubles where specified;
assertions about the real native engine do not substitute those doubles for it.

### Preserved corrections, not suppressed failures

- The first grouped run's subprocess still expected 10 registry providers; the
  three new optional ports make 13. Its expected contract was updated.
- A concurrent-snapshot test tried to patch a method on a slotted native Database
  instance. The injection now patches the class and still invokes the original
  native operation; the old failure and teardown error are retained as evidence.
- The real routed activation fixture initially had no Decision table. It now
  bootstraps the real board schema; the provider also checks every selected table
  before enabling any one-way activation phase.
- Two old Core barrier tests treated the current in-memory Global testing runtime
  as if it were the retired native adapter. Their useful assertion was moved to
  the neutral durable-writer fence in Core and the actual routed native boundary
  in Community. No production fence was weakened or test marked skip.
- One attempted combined-root test command loaded conflicting Core/Community
  fixtures and produced setup errors. Separate repository runs passed; this was
  a test invocation error, not an application defect.
- The first private installed run passed 234 tests and failed one child-process
  check because the isolated dependency path omitted Pydantic. The final harness
  supplies third-party dependencies after the private package path, never checkout
  package paths. Package/source/wheel identity already matched before and after
  that first run; its failure receipt is preserved.

Source receipts under Grafx `.grafx-tmp/`:
`pulse-v006-adoption-regression-first.xml`,
`pulse-v006-adoption-features-qualified.xml`,
`pulse-v006-adoption-core.xml`,
`pulse-v006-adoption-core-write-qualified.xml`,
`pulse-v006-adoption-global-fence.xml`.
Earlier failed checkpoints retain their original filenames; no receipt is
overwritten to present a failed run as successful.

The packaged frontend contains 78 files; its build tree digest is
`9c09e89805e6dc1dc1e2d46995ab8d87986189ca036ef628c7fa395b8f8f7a33`.
Existing Vite chunk-size/Browserslist notices were non-fatal; dependencies were
not changed merely to silence them.

### Final private installed checkpoint — September 13, 2026

**72 passed, zero failures/errors/skips, 153.35 seconds.** This is the focused
closing selection for all four remaining capabilities, their HTTP routes,
history audit authorization, production Global fences and the previously failing
child-process import check. It overlaps the earlier 234 passing installed tests;
unchanged store/transaction/router suites were not run a third time merely to
inflate the count. The earlier broad source run and its focused corrections are
reported above, not relabeled as one all-green full-suite run.

The isolated interpreter imported all three packages from its private installation,
not a checkout or the global Pulse install. All **251 Grafx, 389 Community and
827 Core** package payload files matched source, wheel and installation both
before and after testing. Selected test hashes remained unchanged. Third-party
dependencies came after the private package location; the child subprocess used
the same precedence. `exit_code=0`, `post_test_proof=true` and
`test_inputs_unchanged=true` are recorded in the proof.

| Qualified wheel | SHA-256 |
| --- | --- |
| Grafx 0.0.6, unchanged engine | `d666704a14492d737c43f0293e71605aeaf279aad65b4086d08bcb0160020b70` |
| Community 0.3.3, increments 1–5 | `191922245124776b3df9e0b5cc3689717538cb9ead9273a47e32198b491dbdee` |
| Core 0.3.3, optional neutral contracts | `d5d34fb8c87a6e717f79843e3266f777fa3f8c4014a60be7c3bf2aec88209ed5` |

Receipts and SHA-256 under Grafx `.grafx-tmp/`:

| Receipt | SHA-256 |
| --- | --- |
| `pulse-v006-adoption-regression-first.xml` (initial grouped run, including failures) | `4ffaf2544ba839ce45f87dda0480be93f424692829d7d93a251bb26dc792261f` |
| `pulse-v006-adoption-features-qualified.xml` | `51ec22d5bb9030aba9d2f287dee1440b217e634a38c9304635580ad92812a758` |
| `pulse-v006-adoption-core.xml` | `5a7024458de703aa3d73693c96b576790424e7659e22b0c1702171f08bb0b36e` |
| `pulse-v006-adoption-core-write-qualified.xml` | `f79d608b428c1206cf60cab7f4f66ea5da317658ee5594048148ffc3a89a1571` |
| `pulse-v006-adoption-global-fence.xml` | `1f2f66baeffb928ef39b241e4328dbf10310d80f19b4c85b6613daa2918e4138` |
| `pulse-v006-adoption-installed/installed-qualified.xml` | `a12c650b711c8527eab5e0bc34b19e71fd16ad6169f0130d5f1bd0e52623aa1a` |
| `pulse-v006-adoption-installed/proof-qualified.json` | `c769b7d0f69510d4dc916af75cedb887403fc2fafdad9799e35b3db7423f521e` |

No global installation/restart, production index/history activation, graph reset,
consolidation, redrive or live UI timing was performed. New capabilities are
available to explicit API/port consumers; current substring search and the domain
hybrid pipeline have not been silently switched. The fixed five-item adoption
list is complete at this source/private-package checkpoint, not deployed globally.
