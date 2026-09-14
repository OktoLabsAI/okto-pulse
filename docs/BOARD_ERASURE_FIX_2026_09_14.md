# Board deletion: classified evidence and immutable audit histories

## Defect and correction

The installed 0.3.3 failed to delete the E2E board. The menu/confirmation worked;
the relational erasure transaction omitted dependencies introduced by newer
features. A consistent private SQLite backup reproduced the failure without
modifying the live board.

Community now purges these records while the existing board-scoped erasure
permit is active:

- Code Evidence classification heads, then classification events in dependency
  order, then superseding Code Evidence in dependency order.
- Investigation receipts and semantic subject-version events in dependency
  order, after their head/reference records.
- Semantic assessment v2 findings, metric results and assessment receipts.
- Domain events, including immutable materialized policy/semantic events, after
  the audit authorities that reference them. Handler executions cascade.

Newly covered tables participate in zero-residual verification. Foreign keys,
immutability triggers, board/global writer fences, rollback and the existing
durable physical-erasure continuation remain enabled. No Grafx-specific policy
was added to Core. No schema migration or Grafx change is required.

The live physical phase exposed another integration defect: the routed observer
reported `provider_unavailable / graph_route_binding_missing` after successful
physical erasure removed the binding. It now asks the non-opening Grafx storage
observer for a fresh canonical-scope absence proof. Only a typed, board-matching
`CONFIRMED_ABSENT` result is accepted. Missing binding alone, storage residues,
inspection failures and malformed responses still fail closed; no success cache
or database-opening fallback is used.

## HTTP contract

`DELETE /api/v1/boards/{board_id}` retains success `204` and the existing
authorization/not-found behavior. New structured failures use `detail`:

| Status | `code` | `retryable` | Meaning |
| --- | --- | --- | --- |
| 409 | `board_erasure_busy` | true | Board/global graph writer is busy; wait and retry. |
| 500 | `board_erasure_integrity_failed` | false | Relational erasure integrity check failed; inspect the server log. |

Both include a user-facing `message`, consumed by the existing UI error handler.
SQL, parameters and internal exception details are logged server-side, not sent
in the response. Do not bypass a busy fence or disable SQLite foreign keys.

The first live attempt also exposed a frontend bug: `deleteBoard` used raw
`fetch`, which resolves for HTTP errors. A 409 therefore cleared the selection
as though deletion had succeeded. It now uses the checked `fetchJson<void>`
client, which accepts 204 and propagates failures to the existing error toast
without clearing the selected board or claiming success.

## Verification

- 156 Community tests passed together: relational/physical erasure, evidence
  classification, supersession and receipt lineage, semantic v2, error responses,
  C7 erasure, routed graph diagnosis and rollout privacy.
- 8 Core board-deletion tests passed, including relational-before-physical
  ordering, durable continuation and failure behavior.
- 35 frontend tests passed: deletion 204/403/409/500/network behavior and error
  parsing/toasts. TypeScript and the production frontend build passed; embedded
  assets were regenerated.
- Targeted lint and `git diff --check` passed.
- Full deletion on the private E2E SQLite copy completed with no remaining
  board-scoped rows and zero `PRAGMA foreign_key_check` violations, then rolled
  back. The copy is outside the repository and must not be committed.
- This is focused regression for board erasure, not a claim that the complete
  product test suite or all previous E2E scenarios were executed.

## Installed-instance acceptance

Completed against the installed application and default data home. Only E2E
(`5dcb7b75-466f-4d1e-8893-3899a7cfacf0`) was authorized for deletion.

- The ordinary worker-enabled attempt returned 409 due to Global Outbox
  contention. This exposed the frontend false-success defect described above.
- A controlled browser 409 check with the new frontend preserved `/ E2E`, the
  Board dialog and the sidebar entry, and displayed the error message. That
  specific request was intercepted; it did not perform a deletion.
- The real destructive action used Menu > Board > Delete board and the exact
  E2E confirmation. Background runners were temporarily omitted through the
  worker composition factory in a private maintenance launcher. Authentication,
  routes, graph/storage providers, fences, permits and erasure logic were the
  actual installed implementations, not mocks; no persistent worker setting was
  changed.
- The first physical attempt completed Global Discovery preservation but failed
  the routed missing-binding observation. Its durable continuation was retained.
  After the observer correction, the same UI action resumed that continuation
  and returned **204 No Content**.
- E2E disappeared from the UI; `GET /api/v1/boards/{E2E}` returned **404**.
  The list returned **200** and exactly Okto Neuron, Okto Pulse and Okto Grafx.
- Read-only inspection of every SQLite table with a `board_id` column found no
  E2E rows, including no erasure job or permit. `PRAGMA foreign_key_check` returned
  zero violations. Target uploads, rebuild and evidence directories were absent;
  only an empty board root directory remained, with no graph/binding artifacts.
- The privacy journal (3,955 surviving digests, 3,936 relations, 2 graph Board
  summaries) was removed by the application after manifest verification. The
  graph summary count differs from the three relational boards because not every
  relational board had a global graph summary.
- Both local global installations (Python user site and uv tool environment) were
  updated to Community 0.3.3 with the fix. Core remains 0.3.3 and Grafx 0.0.6.
- Normal `okto-pulse serve` was restored (PID 12328, API/UI 8100, MCP 8101),
  without the temporary worker-factory injection. Startup completed, the board
  list returned 200, and post-restart SQLite checks still showed no E2E residuals,
  no erasure job and no foreign-key violations.

The private SQLite diagnostic backup is retained outside Git. This is not an
automatic restore feature; no other board was deleted to perform the test.
