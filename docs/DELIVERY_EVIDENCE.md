# Delivery evidence: implementation and test verification

Delivery Evidence distinguishes delivered code from the inherited planning
**Code Evidence Matrix**. Each card owns its proof bindings; the Spec aggregates
them. Open a card's Delivery panel or a Spec's **Delivery evidence** tab to see
what is implemented, tested, missing/stale or explicitly waived. The tab explains
the workflow, selects eligible receipts, records many-to-many associations and
shows the immutable audit history. The existing context matrix is unchanged.

## Workflow

1. On a task or bug, use the existing Implementation Target investigation,
   resolution and execution-receipt workflow. Record the clean committed result
   revision, actual file/symbol and explanation.
2. Before completing the task/bug, select its obligations and accepted execution
   receipt in the card's Delivery panel. Explain their relationship once. One
   association can cover several rows. Then submit/complete the card through the
   existing evaluation and transition gates; final rollup credit requires Done.
3. Execute a scenario linked to a **test card**, using the existing authenticated
   Test Evidence V2 runtime. Record authenticated passed or failed results during
   execution; final verification credit still requires a current passing result
   and completed cards.
4. Associate that test with the obligations and the implementation association IDs
   it actually verified. The signed execution must not predate the source
   observation. Several test cards may jointly cover the current implementations.
5. Refresh. `Done` requires implementation and verification for every obligation,
   or explicit phase-specific exemptions. Other existing gates still apply.

Tasks do not provide verification merely by being Done. `automated`, assertions in
free text, an unsigned receipt or a scenario unlinked to a test card do not count.
An authenticated association is the actor's claim about what the test covered;
Pulse validates the signed run but does not independently inspect source code.

### Last batch and execution report

In the execution report dialog, enable **Seal recorded evidence with this report**
to select existing records and optionally reuse their accumulated impact. Enable
**Save a last batch together with this report** to prepare progress entries or
associations to accepted execution receipts/authenticated test runs using the
existing forms. Each added entry appears in the unsent list and can be removed.
These entries are local drafts until the final submit; closing the dialog discards
them. Intermediate checkpoints can still be saved from the Delivery panel.

The final submit uses the same card-scoped REST endpoint with
`card-delivery-report/v1`. The server saves all new entries, seals them together
with the selected existing records and submits the existing report in one
transaction. Permissions, report scores, source verification and transition gates
still apply. A rejection saves none of the batch and keeps the draft visible.
An unchanged retry reuses the request key; changing the report creates a new
request. Refresh and review conflicting versions instead of silently rebasing a
draft. At most 50 new entries and 200 selected records are accepted; the report
also has an aggregate size limit enforced by the server.

This mode is available from started/in_progress for transitions requiring an
execution report. Test Card→Validation continues through its existing flow without
an execution report. The UI associates existing proofs; creating source execution
receipts inline remains available through the canonical REST/MCP contract. None
of these operations auto-approve evidence or relax independent review.

## Scope, currentness and exceptions

The server inventories FR, TR, BR, AC, API contracts, integration/observability
requirements and decisions. It hashes each obligation's semantic content, excluding
operational task links, state and timestamps. Cancelled, superseded, revoked and deprecated
items are excluded. Legacy string entries use index-based refs and content digests;
reordering them requires reviewing the affected links. If the inventory is empty,
a Spec-root obligation requires an explicit decision instead of vacuous 100%.

Proof is bound to Board, Spec edition and obligation digests. Tests also reference
the concrete implementation association IDs. The current target revision/latest
execution and non-revoked accepted receipt must still match. A later failed test,
tampered signed evidence, lost card/scenario ownership, archived card or changed
obligation invalidates the relevant proof. A new implementation for an already
covered obligation needs verification too; it does not inherit the old result.

An investigation receipt's observation TTL does **not** erase the historical fact
of a committed delivery. Its explicit revocation and the current Target execution
still govern admissibility. This is different from fresh-source investigation.

Authorized humans may waive **implementation** or **verification**, separately,
for exact obligations with justification. Exemptions display as waived, never as
implemented/passed. Without code, decide both phases explicitly. Revocation appends
an audit tombstone, not an edit/delete. Old done Specs are not reopened by reads;
authorized users may add missing associations retrospectively.

The existing board `delivery_evidence_gate` setting is `advisory|blocking`, with
blocking as the fallback. The authorized Spec `skip_delivery_evidence` override
affects the Spec transition. Neither changes the factual rollup to verified.
These are distinct from planning Code Evidence skips. Existing installations need
the paired Core and Community update; a required missing adapter fails closed.

## API and agent contracts

REST GET `/api/v1/boards/{board_id}/specs/{spec_id}/delivery-evidence`.
GET returns edition/version, obligation rows, eligible receipts, proof IDs,
blockers, currentness rejection IDs, `per_card` obligations and audit records.
Proof POST is card-scoped:
`/api/v1/boards/{board_id}/cards/{card_id}/specs/{spec_id}/delivery-evidence`:

```json
{
  "expected_spec_edition": 2,
  "expected_card_version": 7,
  "idempotency_key": "task-42-delivery-1",
  "kind": "implementation",
  "obligation_refs": ["fr:fr_42"],
  "execution_id": "accepted-execution-id",
  "justification": "Implements the parser's input contract."
}
```

Kinds: `implementation` uses an execution ID; `test` uses a TEST-card/scenario ID
and nonempty card-ledger `implementation_ids`; human-only `revoke` uses `record_id`
and empty obligation refs. The card ID comes from the path. All use Spec edition,
card policy version, justification and actor-scoped
idempotency. Maximum 1,000 refs/implementation IDs and 20,000 justification
characters. Unknown/server-owned fields, duplicates and incompatible field
combinations are rejected. Identical replays return the same `{id,replayed:true}`;
changed replay payloads or stale versions return 409, missing targets 404,
invalid proof 422. UI retries reuse the key for an unchanged uncertain request.

The old Spec POST remains only for human-authorized `waiver` and legacy `revoke`,
with `expected_edition`/`expected_version`. Waivers require `phase`, exact refs
and justification. Legacy revocations require `record_id` and empty refs.
New Spec-scoped `implementation`/`test` requests return 422
`delivery_card_scope_required`, with guidance to the card writer. Old payloads
are not silently translated: they lack the card-version fence. Historical proof,
waivers and revocation records are preserved; this change performs no data migration.

MCP: `okto_pulse_get_delivery_evidence(board_id,spec_id)` and
`okto_pulse_record_delivery_evidence(board_id,card_id,spec_id,evidence={...})`.
The latter uses the same body as REST and the same authorized use case/store.
Resources and tool documentation teach the mandatory closeout sequence:
`reference/code-traceability`, `reference/tool-docs/code-traceability`,
`reference/tool-docs/test-scenario`, `reference/tool-docs/spec`, `reference/spec_gates`,
`workflows/cards`, `workflows/specs`, plus initial agent instructions and generated
tools catalog. Agents cannot self-waive.

| Operation | Required authority |
| --- | --- |
| Read | Board access + `code_traceability.evidence.read` |
| Implementation association | `code_traceability.target.execution_submit` |
| Test association | `spec.tests.execute` (QA need not get code-write rights) |
| Waiver | Authenticated human + `code_traceability.waiver.create` |
| Revoke | Authenticated human + `code_traceability.waiver.clear` |

The MCP dispatch policy admits board readers and the use case separately checks
the selected kind's mutation authority. Never treat dispatch admission as write
authorization. No request can assert actor identity, validity or authorization.

## Persistence and verification

Community stores card proofs in `card_delivery_evidence_records`. The legacy
`delivery_evidence_records` keeps historical proofs and human exceptions; the
Spec rollup consumes card proofs and active legacy waivers. Composite scope
indexes and actor/key uniqueness support replay and reads. SQLite guards reject
cross-board inserts and audit rewrites/deletes while the parent exists. No new
graph table or Grafx-specific dependency enters Core.

Spec completion checks the same evaluator as allowed transitions and checks again
under the transaction write fence. Receipt mutation and delivery writes share the
board fence; row-locking databases also lock loaded proof rows. Failed proof cannot
be admitted solely because an earlier preview passed.

Tests: Core domain/contracts/lifecycle gates, Community real SQLite and signed Test
Evidence, duplicate-writer replay, immutable audit, REST/MCP closed inputs and
authorization, UI unit tests and Chromium workflow fixtures. All use isolated
data; neither existing records nor the active user's Pulse are changed by testing.

When testing paired worktrees, set **both** `OKTO_PULSE_CORE_REPO` and
`OKTO_PULSE_COMMUNITY_REPO` to the intended checkouts. `PYTHONPATH` alone is not
enough: the test bootstrap deliberately resolves and normalizes the paired repos.

### Historical validation recorded on 2026-09-14

These results describe the earlier implementation, not a rerun of v0.4.0.
Current work and validation are tracked in the paired implementation ledger
linked from [pulse-simplification/README.md](pulse-simplification/README.md).

| Selection | Result |
| --- | --- |
| Core delivery/domain/lifecycle + existing coverage/transition regressions | 125 passed |
| MCP contracts, permissions, resources, catalog and Spec validation regressions | 302 passed |
| Core import-boundary governance + repeated delivery tests | 61 passed (overlaps the first selection) |
| Community delivery, REST/MCP, existing traceability transports, signed Test Evidence | 98 passed |
| Schema upgrade preserving existing Done Specs, repeated initialization | 1 passed |
| Existing Code Traceability persistence regressions | 10 passed |
| Frontend delivery + existing Code Traceability panels | 37 passed |
| Chromium delivery association/refresh + context matrix references | 2 passed |

Python lint and the new frontend files' lint pass. The repository-wide frontend
lint ratchet is **not green**: 399 existing warnings exceed its 393-warning
baseline. The changed existing files retain the HEAD counts (SpecModal 19, API
service 31); new delivery files add zero warnings. This is recorded rather than
raising that baseline or claiming the global lint passed.

Production frontend build and packaged-tree verification passed: 78 files,
SHA-256 `5fd0b95e7ca08e287c51722fd7166328604d24cdf5770a653a49cb2461f60535`.
The active user installation was not restarted or replaced.

Publication integration with issues #84–#88: 55 Core, 42 Community and 11 frontend
focused tests passed again. The combined production build contains 78 files,
SHA-256 `a0ecae10bcc25ee7cba773574300054b00cb3fbea68fa7de11b80e4add64cbb1`.
