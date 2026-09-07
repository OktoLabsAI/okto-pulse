# Board DLQ redrive from the UI

The Dead Letter Inspector offers a row-level **Redrive** and a separately
confirmed **Redrive all**. Both call
`POST /api/v1/kg/queue/dead-letter/redrive`; simply opening the inspector is
read-only. This operates on the board consolidation DLQ, not the independent
Global outbox, policy DLQ, canonical debt or cognitive pending ledger.

## Authorization and scope

The REST boundary resolves the authenticated actor, realm and board writer
access before constructing a downstream operation. Missing, foreign and
viewer-only boards retain the same non-enumerable 404 response as other KG
writer routes. The shared Core use case additionally enforces
`kg.operations.queue.reprocess`; UI permissions are not the security boundary.

Supply exactly one of `dead_letter_ids` (1–200 IDs) or `redrive_all: true`.
Selected rows carry `scope: generic | code_traceability`. All mode lists only
accessible rows and partitions each page into these same scopes. Code
Traceability authorization is enforced by the existing shared use cases, not
bypassed by selecting all. Backend-specific graph code is not introduced into
Core.

## Finite, best-effort execution

Each transaction has at most 200 IDs. All mode admits at most the initially
visible row count and attempts each ID at most once per request. This is a
cardinality bound, **not an immutable snapshot of the original IDs**: concurrent
arrivals may replace earlier rows. It does not keep draining indefinitely when
workers produce new failures. Aggregate response/ID tracking memory is bounded
by the initial count, not by one page.

The response reports selected/requeued/already-queued counts, `remaining`,
`batches` and `stop_reason`. Reasons include `initial_selection_limit`,
`repeated_rows`, `blocked`, `no_progress` and `refused`. Remaining visible rows
or a refused batch prevent a success response. Counts are observations, not a
promise that another worker cannot change the queue immediately afterward.

The existing `board_id+artifact_type+artifact_id` idempotency and recovery
classification rules remain in the shared service. Transactions commit per
scope/batch; a later failure does **not** roll back earlier successful batches.
The UI refreshes after success, partial refusal or HTTP failure, and never
equates an unsuccessful response with successful completion.

`process_now` defaults to true. Only a mutated result wakes the worker. An
already running worker is signalled; otherwise the existing one-batch runtime
worker entry point is used. Requeued means admitted for processing, **not** that
cognitive consolidation or Global projection is finished. `process_now: false`
skips this explicit wake, but does not suspend an independently running worker.

## Validation and deployment

2026-09-07: isolated staged Community sources passed 133 tests across REST
contracts, operational authorization and governed enqueue fences (42.30 s).
The final response-refusal guard added one case; its focused 14-test REST slice
passed (16.99 s). These runs overlap and are not summed. Core's existing DLQ
and authorization slice passed 80 tests (7.18 s). UI DLQ plus accumulated
Settings coverage passed 31 tests (5.79 s). Ruff, staged whitespace, TypeScript
and production Vite build passed.

Fixtures cover 451 rows in 200/200/51 batches, continuous arrivals, repeated IDs,
blocked/no-progress/refused responses, scope partitioning, selection validation,
worker signalling/direct batch, permission denial, confirmations and refreshing
after an HTTP failure. They do not mutate the live board. Live redrive and
consolidation are intentionally not used as deployment smoke tests: the 21
pending specs remain reserved for benchmarks.

Accumulated source deployment: Pulse PID 24152 replaced PID 18920 only after
terminal shutdown and both ports were free; graph close reported zero failures.
The 78 packaged assets match the build (tree SHA256
`373c5fe492565aa1e7769354072da7eb7977414b16a94e6078579b04fd15461c`).
API 8100 and MCP 8101 are owned by the new PID. `/health` reports healthy/0.3.3;
runtime Settings returns Grafx and page size 8192, with no pending restart.
The browser serves `/assets/index-igFxIQ1X.js`, including the final refusal copy.
KG displayed 500 of 2779 canonical nodes. The live board inspector returned an
empty consolidation DLQ and disabled Redrive all; Settings/inspector were closed
without saving or submitting any redrive. Empty consolidation DLQ is not a
claim that Global/policy DLQs or cognitive pending items are empty.

The cognitive ledger remains byte-identical (SHA256
`4AFF1AB6EE6C6E621C6598148154A04298500B8DA92EBF0F5E90AD081B2217F4`).
This deployment loads source checkouts, not a newly published/installed wheel.
Cold KG latency and end-to-end write performance remain separate open work.
