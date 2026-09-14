# Installed Pulse acceptance on the new E2E board — 2026-09-14

## Verdict

Follow-up: [Citation correction and SQLite diagnosis](REFINEMENT_CITATIONS_SQLITE_DIAGNOSIS_2026_09_14.md)
records the installed fix for F1 and a real-adapter reproduction of the SQLite
contention mechanism. The remaining end-to-end delivery flow is not yet approved.

**Partial acceptance; not a complete end-to-end approval.** The installed UI,
MCP and REST surfaces passed the checks below, but the governed inherited-evidence
flow exposed a version/currentness problem and a write request encountered
intermittent SQLite contention. The happy path through inherited evidence,
derived Spec and completed Task/TEST delivery is still pending.

This is a new board, not the previously deleted E2E. Its data was preserved.
No other board was modified, no policy gate was disabled, and Pulse was not
restarted. No source implementation was changed in this acceptance run.

## Runtime and fixtures

- Board: `97bb889c-ce45-4148-9ed3-7eebc04c8316`, created by the user.
- Installed Community/Core: 0.3.3; Grafx: 0.0.6 with NumPy acceleration.
- Community baseline: `9a87ac670bd9d75ab7e9189ad531eff8f428378c` plus the
  pending, installed board-erasure fixes documented separately.
- Paired Core baseline: `6faf6fe`; runtime PID 12328, API/UI 8100, MCP 8101.
- Direct draft Spec: `daf89c39-652e-4206-943f-0cf1205fa8c1`, edition 1,
  final version 7, Project Structure revision 2.
- Ideation: `de5b3e81-2a35-4c1a-acfc-6c2b4882c1e1`, Done, edition 2, version 12.
- Refinement: `245b65f4-edb6-49ed-acd8-858e4608441c`, left Draft, edition 1,
  version 2 for reproduction of the evidence finding.
- Architecture: `b40d650d-0584-4ab6-aee8-3f81201d0f4c` on the direct Spec.

## Live results

| Area | Result and scope |
| --- | --- |
| Board/agent access and authoring | New board initially empty; guidelines and policies read. Spec, Ideation, Refinement and architecture persisted through public APIs/MCP. |
| Project Structure creation | Three AS-IS nodes created atomically and displayed in the UI with notes. |
| Stale mutation rejection | Old Spec/revision fences rejected without changes. A real UI edit held across a concurrent Spec change received HTTP 409, retained the user's input, required Refresh & review, then saved through explicit retry. |
| Invalid batch rollback | A batch first changed a note, then attempted to parent a node under a file. It was rejected. Readback confirmed all three original nodes, the original root note, no invalid child, version 7/revision 2 and the same digest. |
| Architecture margins | Two connected nodes at negative/positive coordinates rendered fully after Fit to view. Visible margins were at least about 109 CSS pixels; scrollable surface margins exceeded 512 pixels on all four sides. |
| Lineage direction | Live Ideation → Refinement edge starts at x=240, outside the source's right edge at x=236, and ends at x≈382.7 before the target at x≈386.7. It no longer exits left. |
| Lineage status colors | Done border `rgb(34,197,94)` and Draft border `rgb(148,163,184)`, with corresponding distinct badges. This fixture tests two statuses, not every possible status. |
| Direct evidence links | One accepted V2 AS-IS evidence record linked to exact FR/TR/AC IDs; all three mappings persisted. Direct evidence is intentionally outside the inherited Code Evidence Matrix, so this is not a positive test of populated matrix cells. |
| Delivery Evidence negative gate | MCP and UI agree on seven obligations without implementation/TEST proof; `allowed=false`, missing implementation and test-result blockers, empty association disabled. No Task was used as a substitute for TEST verification. |
| Lifecycle/quality freshness | Ideation followed supported transitions to Done. Returning to Draft opened edition 2, invalidating the earlier assessment; a new assessment was required. Resource N/A marks were only accepted in Draft. |
| Grafx graph/API reads | Graph and stats returned HTTP 200, no failed edge tables. Keyset pagination at limit 3 returned 15 distinct nodes across five full pages and an empty terminal page, without duplicates. These are small-fixture checks, not a large-graph benchmark. |
| Knowledge Graph UI | Graph opened and displayed nodes. Global Discovery loaded 14 intents in five categories. Key Decisions executed successfully and returned zero rows, consistent with this fixture having no Decision nodes. Positive decision retrieval remains outside this run. |
| Cognitive Action Center | Opened from KG Health; purpose, policy explanation, filters and empty state loaded. Zero readiness records. No waiver, redrive or artificial processing failure was created. |
| KG recovery presentation | Active board/discovery backend displayed as Okto Grafx. No legacy recovery running; no online rebuild was attempted. |

Final structure digest:
`cf9f4ea108ddbb582a65732b29f3f34f991222fccf34ceb5602722452007bb60`.

## Findings preventing full approval

### F1 — Citing an evidence ID invalidates the version required for that evidence

Reproduced through ordinary MCP operations on the Refinement above:

1. Start investigation for version 1 and submit an accepted contextual V2 receipt
   with outcome `evidence_applicable`.
2. Submit truthful `current_implementation` evidence:
   `code_evidence_bca2dc834e8f427ca7a1ace20c3bfe8f`, receipt
   `code_receipt_94427b025aff40209678d8e734f88384`, both bound to version 1.
3. Update the analysis to cite the returned evidence ID. The Refinement becomes
   version 2.
4. Read full context: the evidence remains active and referenced, but gate
   `refinement_evidence` reports `passed=false`,
   `code_evidence_materiality_link_required`, `active_evidence_count=0`, with
   that ID in `unmapped_referenced_evidence_ids`. Investigation outcome becomes null.

The board's existing advisory policy returns `allowed=true`, but this was not
used to label the flow successful or push the Refinement to Done.

Source mechanism in the paired Core:

- `services/main.py`, `update_refinement`: `analysis` is a version-bumping field.
- `application/use_cases/code_traceability.py`: gate reference IDs are extracted
  from the analysis text.
- `services/code_traceability_gate.py`, `_refinement_evidence_blockers`:
  candidates require `parent_version == context.subject_version`; current
  receipts also require the exact subject version.

Each individual fence is understandable, but the composition needs a supported
way to attach material references without invalidating its own attestation.
Do not solve this by accepting arbitrary stale receipts or dropping version fences.
The existing unit tests exercise compatible same-version fixtures, not this
authoring sequence. A lifecycle integration regression is needed before correction.

The inspected source file was `frontend/src/components/specs/ProjectStructureTab.tsx`,
SHA-256 `0824487a4b512dd0594d851bc27f015da7c85b847bd240e71941bd53c54425a0`.
This was an authenticated agent observation of existing source, not a claim that
Pulse independently verified code or that a new implementation was delivered.

### F2 — Intermittent authoring lock failure

The first `start_code_investigation` request failed after roughly 30 seconds with
`code_investigation_persistence_error` /
`code_investigation_admission_lock_failed`, exposed as non-retryable. After
read-only diagnostics, a controlled retry with the same idempotency key succeeded
in approximately 5.25 seconds. One evidence write took approximately 24.5 seconds.
Retry success does not resolve the underlying contention.

Application logs report long-lived `core.global_outbox.batch` session checkouts
(including ages above 500 seconds). Process samples showed concurrent Grafx
reads/commit work. These observations do **not** establish which SQLite
transaction owned the writer lock. Precise ownership still needs diagnosis.

Adapter reference: `sqlalchemy_code_traceability.py`, admission lock implemented
as the board-row UPDATE. Logs inspected:
`D:/Projetos/Techridy/pulse-install-20260914/board-delete-final.stderr.log` and
the corresponding stdout log. No background process was killed to hide the failure.

### Health qualification

At 15:35 UTC, MCP reported `graph_state=healthy` and `overall_state=healthy`,
but `metric_status=unavailable` and native budget `routed_budget_incomplete`.
The UI likewise displayed Healthy alongside unavailable telemetry. Graph reads
worked; this is not evidence of corruption, nor proof of complete health telemetry.
The agent instructions explicitly caution against treating unavailable metrics
as a fully healthy result. This discrepancy remains recorded, not silently waived.

## Focused automated regression rerun

| Suite | Result |
| --- | --- |
| ProjectStructureTab, CodeTraceabilityPanels, LineageGraphModal, ArchitectureDiagramEditor, CognitiveActionCenterView | 99 passed, 5 files, 69.46 s |
| Core `test_code_traceability_gate.py` and `test_contextual_code_investigation_outcomes.py` | 27 passed, 3.18 s |

**126 tests passed.** This is not a fresh complete repository regression and does
not supersede the live findings. Existing broader evidence is documented in
[the integration report](LOCAL_REMOTE_INTEGRATION_2026_09_14.md) and
[the erasure acceptance report](BOARD_ERASURE_FIX_2026_09_14.md).

## Remaining acceptance work

1. Resolve and regression-test F1, then complete inherited evidence → derived
   Spec → visible FR/TR/AC matrix cells using the public lifecycle.
2. Diagnose and address F2 without weakening transaction/board fences, then
   repeat authoring while legitimate background graph work runs.
3. Complete the positive implementation evidence + executed TEST-card delivery
   flow. Only its missing-proof rejection was validated live in this run.

The new E2E board and its fixtures remain available. No deletion, reset, forced
completion, policy change, commit or push was performed in this run.
