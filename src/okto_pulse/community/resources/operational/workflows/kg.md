## Community runtime notes

The protocol above is loaded from the Core catalog, not copied into this file.
These notes describe the Community mechanism; they do not add permissions or
change query, evaluation, evidence or completion gates.

Community uses Okto Grafx for the Board graph and Global Discovery, SQLite for
operational ledgers, and the local embedding provider for semantic retrieval.
Board storage lives under the configured KG root (`boards/{board_id}/grafx/`).
Global Discovery resolves its active generation under the configured data home;
do not assume a fixed file is the active generation or alter it directly.
The daily decay tick is scheduled by the Community scheduler (03:00 UTC).

### Transparent Grafx checkpoint recovery

The routed Board lifecycle handles Grafx's explicit `recovery_required` latch
without operator interaction. The path remains deliberately narrow and runs
while Pulse owns the Board's exclusive lifecycle window and Core write fence.
Both `CHECKPOINT` and `FSYNC` use that exclusive window because either can enter
recovery. Pulse snapshots the WAL durably, closes the pooled handle, lets Grafx
perform its native writable-open recovery, verifies the complete database,
performs a cold-reopen probe and retries the checkpoint exactly once.

`GrafxUnsupportedOperation` does not imply durable damage and never triggers WAL
recovery. In particular, an unleased process-local handle already marked closed
is discarded and ordinarily reopened by the shared pool. A closed handle that
is still leased is an invariant failure and remains fail-closed; it is never
closed or replaced underneath its transaction.

A successful native pass may report `recovered`, or `skipped` with the exact
reason that no WAL work was found; both mean the stale process-local latch was
removed and the database passed verification. Missing storage, an unclean
verification, recovery-policy refusal, lost fence, close/open failure, a second
checkpoint failure, or any unsupported-operation error remains fail-closed.
This mechanism never rebuilds, purges, replaces a generation or falls back to
another graph backend. Failures outside this bounded automatic case must be reported to the operator;
no public recovery executor is exposed.

### Delivery-ledger catch-up in Community

If a later sweep finds that the same governed child was already drained, it
reconstitutes the child with the original immutable `intent_created` timestamp;
the sweep observation time must not redefine transition identity. Settlement
then uses the current delivery-ledger owner. Existing delivery debt is retained.
An owner already in `outbox_persisted` or `delivered` is conservatively reopened
as parity-refresh debt because the graph may have changed before a relational
rollback. Any in-flight outcome for that physical attempt is fenced, and the
tick-owned redrive publishes only a fresh `attempt:n+1` key. Never rearm or
rewrite an older governed outbox row manually.

Health may report `global_outbox_dead_letter_backlog` separately from active
queue depth, cognitive work and canonical debt. It identifies unavailable
delivery, not a public repair action or an exemption from product gates.
Common health/retry rules remain in `okto-pulse://reference/kg-health` and
`okto-pulse://reference/errors`.
