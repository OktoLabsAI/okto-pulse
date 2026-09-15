# Delivery evidence: implementation and test verification

This 0.3.3 development feature distinguishes delivered code from the inherited
planning **Code Evidence Matrix**. Open a Spec's **Delivery evidence** tab to see
what is implemented, tested, missing/stale or explicitly waived. The tab explains
the workflow, selects eligible receipts, records many-to-many associations and
shows the immutable audit history. The existing context matrix is unchanged.

## Workflow

1. On a task or bug, use the existing Implementation Target investigation,
   resolution and execution-receipt workflow. Record the clean committed result
   revision, actual file/symbol and explanation. Complete the card.
2. In Delivery evidence, select the obligations and the accepted implementation
   receipt. Explain their relationship. One association can cover several rows.
3. Execute a scenario linked to a **test card**, using the existing authenticated
   Test Evidence V2 runtime; record `passed` and complete the test card.
4. Associate that test with the obligations and the implementation association IDs
   it actually verified. The signed execution must not predate the source
   observation. Several test cards may jointly cover the current implementations.
5. Refresh. `Done` requires implementation and verification for every obligation,
   or explicit phase-specific exemptions. Other existing gates still apply.

Tasks do not provide verification merely by being Done. `automated`, assertions in
free text, an unsigned receipt or a scenario unlinked to a test card do not count.
An authenticated association is the actor's claim about what the test covered;
Pulse validates the signed run but does not independently inspect source code.

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

There is **no new Skip setting**. Planning/test Skip flags, greenfield and advisory
mode do not bypass this delivery gate. Existing installations need the paired Core
and Community update; an edition without a delivery adapter fails closed.

## API and agent contracts

REST GET/POST `/api/v1/boards/{board_id}/specs/{spec_id}/delivery-evidence`.
GET returns edition/version, obligation rows, eligible receipts, proof IDs,
blockers, currentness rejection IDs and audit records. POST accepts:

```json
{
  "expected_edition": 2,
  "expected_version": 7,
  "idempotency_key": "task-42-delivery-1",
  "kind": "implementation",
  "obligation_refs": ["fr:fr_42"],
  "card_id": "task-id",
  "execution_id": "accepted-execution-id",
  "justification": "Implements the parser's input contract."
}
```

Kinds: `implementation` uses card/execution IDs; `test` uses TEST-card/scenario IDs
and nonempty `implementation_ids`; `waiver` uses `phase`; `revoke` uses `record_id`
and empty obligation refs. All use edition/version, justification and actor-scoped
idempotency. Maximum 1,000 refs/implementation IDs and 20,000 justification
characters. Unknown/server-owned fields, duplicates and incompatible field
combinations are rejected. Identical replays return the same `{id,replayed:true}`;
changed replay payloads or stale versions return 409, missing targets 404,
invalid proof 422. UI retries reuse the key for an unchanged uncertain request.

MCP: `okto_pulse_get_delivery_evidence(board_id,spec_id)` and
`okto_pulse_record_delivery_evidence(board_id,spec_id,evidence={...})`.
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

Community adds `delivery_evidence_records` through the existing `create_all`
schema lifecycle, without modifying existing Spec/card records. Composite scope
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

### Validation recorded on 2026-09-14

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
