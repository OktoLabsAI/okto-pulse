# Installed Community 0.3.3 E2E checkpoint — 2026-09-14

The subsequent run on the newly created E2E board is documented in
[New-board installed acceptance](E2E_NEW_BOARD_2026_09_14.md). It is a separate
fixture and a partial acceptance, not a replacement of this historical record.

## Subsequent authorized deletion

The user clarified that their observed failure was board deletion, not a claim
that E2E was invalid, and authorized fixing and deleting E2E. That targeted
delivery is now documented in [Board erasure fix and live acceptance](BOARD_ERASURE_FIX_2026_09_14.md).
E2E was deleted through the UI (204), with no residual erasure job or SQLite
foreign-key violation and all three other boards preserved. The earlier findings
below remain historical observations, not current E2E availability or a blanket
diagnosis of board invalidity.

## Outcome: blocked, not an end-to-end approval

The user started the updated global installation and granted access to the existing
E2E board. This run uses the live HTTP/MCP surfaces, not the disposable instance
from the preceding session. No server was stopped or restarted by this test run.

- Community source: `9a87ac6`, `feature/v0.3.3`.
- Paired Core: `6faf6fe`, package version 0.3.3.
- Installed Grafx: 0.0.6, acceleration dependencies import successfully.
- Runtime: Python 3.13.1, PID 14832, API 8100 / MCP 8101.
- E2E board: `5dcb7b75-466f-4d1e-8893-3899a7cfacf0`.
- Installed files rechecked byte-for-byte against the installation wheels:
  393 Community files and 832 Core files identical.
- MCP manifest: version 0.3.3, 340 tools. Its build commit is `unknown`; the
  source attribution above comes from wheel/source verification, not the manifest.

## Live checks

| Check | Observed outcome |
| --- | --- |
| MCP identity, board access, guidelines, unseen summary, Spec inventory | Success; E2E contains eight historical Specs, not a new empty board. |
| UI board selection, Spec list and existing Spec modal | Success; eight Specs rendered. |
| Delivery Evidence REST + MCP + UI | Success on Spec `7d5a8147-feed-4789-af21-ecc70c98b8eb`: 19 obligations, zero proof records, `allowed=false`, implementation/test missing blockers. UI reports incomplete delivery and disables an empty association. No historical Spec was reopened. |
| Existing Spec Validation UI | Current edition 4 result loads separately from Previous; five metrics, justifications and a pinpoint render. This is readback of historical evidence, not a new execution. |
| Project Structure UI | Draft Spec `0982d8c7-ea61-4db0-b917-ff06c72c1ecc` exposes the tab, View/Edit controls, revision 0 and the unauthored state. An older validated Spec with no tree hides the tab by the current policy. No tree was changed. |
| Code Evidence Matrix UI | Empty inherited-evidence state renders on the inspected direct Spec. Populated reference-cell behavior is covered by automated tests below, not proven by this live fixture. |
| Cognitive Action Center UI | Purpose/help, filters, eight attention records and four historical failures load. No waiver, redrive or cognitive mutation was submitted. |
| KG Health UI + MCP | `at_risk`, telemetry unavailable, Board `Not bound`, reason `graph_route_binding_missing`; transient materialization timeouts also observed. Not a healthy graph result. |
| Diagnostic rebuild preflight | `diagnostic_complete`, 25 eligible sources, 7 cancelled, 2 legacy unknown; `manifest_ref=null`, `source_set_hash=null`, offline executor required. |
| Online confirmation using returned preflight hash and null manifest | Expected HTTP 409 `recovery_execution_required`, not framework 422. No confirmation capability issued; no run attempted. |
| Installed `kg subtype` with no subcommand | Help and exit 1, no unset-handler traceback. |
| Installed graph export with nonexistent parent | `export_output_error`, exit 2, destination parent remains absent. Validation occurs before database setup. |

## Blocking findings

### B1 — Existing E2E graph has no persisted route binding

The board directory is present but empty. Health reports `Not bound` and no
current generation. MCP `kg_query_cypher` refuses the routed operation. No graph
bootstrap, binding fabrication, reset, schema migration or legacy DLQ redrive was
attempted. An empty directory is not sufficient authority to discard historical
relational provenance or bypass the governed recovery protocol.

The three read endpoints handle the same unavailable board inconsistently:

- `GET /api/v1/kg/boards/{board}/graph?limit=10`: HTTP 500, legacy
  `kuzu_error`, routed operation refused.
- `GET /api/v1/kg/boards/{board}/stats`: HTTP 500, plain Internal Server Error.
- `POST /api/v1/kg/boards/{board}/cypher?...`: HTTP 503,
  `graph_capability_unavailable`.

The query failure is real, but the route-binding absence is distinct from
Grafx corruption. Error projection should be consistent and backend-neutral.
Recovery is not authorized merely by a generic `at_risk` label.

### B2 — SQLite contention blocks normal authoring

One MCP `create_spec` request for a new `[E2E 20260914]` synthetic fixture failed
with `sqlite3.OperationalError: database is locked` while executing the board
write-fence UPDATE. The following MCP inventory and UI list still showed the
same eight Specs and no requested new title. The request was not blindly retried.

Read-only process stack samples found background graph work: exact cosine search
in `grafx_board_vector_search._exact_hits`, Global Outbox graph reads, and later
checkpoint/index adoption. These samples establish concurrent expensive work,
**not** which transaction owned SQLite's writer lock. Determining its precise
owner remains necessary before calling this root cause proven.

No recent redirected application log was found in the usual logs directory.
The report relies on actual MCP/REST responses and stack samples, not an invented
application-log diagnosis. A request-context connection reset after a generic 500
did not reproduce with browser `fetch`: Delivery Evidence returned HTTP 200.

## Focused automated regression in this run

These tests use disposable fixtures and paired source, not live board mutations.
They complement, and do not replace, the blocked installed end-to-end flow.

| Group | Result |
| --- | --- |
| `test_issue_84_88_regressions.py`, `test_cli_kg_subtype.py`, `test_delivery_evidence_integration.py` | 43 passed in 54.95 s |
| Cognitive badges, Cognitive Action Center, Delivery Evidence, Code Traceability panels, Architecture Diagram Editor, Lineage Graph Modal | 103 passed, 6 files, 75.48 s |
| Project Structure tab and model | 12 passed, 2 files, 9.89 s |

Total: **43 Python + 115 frontend tests passed**. This is a focused rerun, not a
fresh full regression. The earlier 749-Python / 1,871-frontend integration evidence
remains separately documented in `LOCAL_REMOTE_INTEGRATION_2026_09_14.md`.

## Remaining live acceptance work

1. Diagnose and resolve the SQLite writer contention without weakening fencing.
2. Review the supported E2E graph initialization/recovery path. A full recovery
   requires a separately coordinated offline window, physical-copy rehearsal and
   installation fingerprint; it was not executed while other boards were live.
3. Repeat synthetic authoring, Project Structure atomic edits/stale fences,
   planning evidence links and delivered-code / signed TEST-card associations.
4. Verify live graph ingestion, paginated nodes/edges, discovery results and
   cognitive closeout after the E2E graph is operational.
5. Exercise populated architecture margins and lineage handles/status colors on
   new fixtures; current evidence for those changes is automated regression only.

Cognitive closeout: **blocked** by the unavailable E2E route; no synthetic
consolidation session or successful-delivery claim was created.
