# Recovery write batching — first wave

2026-09-09. Follows checkpoint `5e13e45` on
`fix/v0.3.3-grafx-transparent-recovery`. This wave changes only Community;
Grafx stays at the validated 0.0.5 and Core has no new engine dependency.

## Global candidate implementation

The private Global Discovery candidate now groups up to 128 digests per chunk.
Each chunk prepares/validates its vectors, checks its Board endpoint and source
identities, then uses native `Transaction.executemany` for digest insertion and
for links. Both calls and the endpoint readback share **one write transaction**.
Link parameters contain only IDs, not redundant embeddings. Input ordering and
the deterministic digest IDs/timestamps remain unchanged.

The old per-digest upsert path is not replaced for live outbox operations, privacy
survivor restoration or general callers. This insert-only path belongs to a fresh
unpublished recovery candidate whose complete source inventory was authenticated
by the coordinator. It refuses existing/duplicate source identities rather than
silently updating unrelated candidate contents.

## Guarantees

- Fences are rechecked during preparation, before each admitted write and before
  commit. Cancellation/fence loss never promotes a partial generation.
- Failure during either executemany or endpoint validation rolls back the entire
  chunk, not only the last executemany savepoint.
- Both `durable` and `wrote` must be true before acknowledging the chunk.
- Earlier chunks can be durable in a failed candidate. The existing whole-candidate
  verification, checkpoint/cold readback and pointer cutover remain mandatory.
  The current active graph is not overwritten by batch ingestion.
- No retries/splitting after a commit attempt, durability ambiguity, corruption,
  authority loss or failed rollback.
- No changes to native OCC, leases, WAL/durability, snapshots, same-board saga
  serialization, Global lifecycle lock or Core interface requirements.

## Limits and compatibility

The initial maximum is an internal 128-digest bound, not a new Settings knob. It is
not a byte/RSS limit; native memory/transaction/query budgets remain enforced.

If a native transaction/query budget refuses a chunk **before commit** and rollback
completes, the adapter splits it into smaller chunks. At one digest, it can use the
existing fenced upsert + link path, allowing configurations such as one staged row
per transaction to continue working. It never raises a configured quota. Therefore
the one-commit-per-chunk expectation applies when budgets admit the normal path;
the conservative fallback intentionally gives up that optimization.

## Evidence

Fixed native tests measure actual write-commit reports, excluding read-only
transactions. A five-digest case with a test-only chunk bound of two uses four
write commits: one Board summary plus three chunks. The production-sized bound
has the normal-path formula `1 + ceil(D / 128)` per Board with `D` digests,
excluding schema/index setup, versus the former `1 + 2*D`. A standalone three-digest
chunk uses one write commit for all three digests and links.

This is commit-count evidence, **not a measured wall-time speedup**. Full recovery
also performs source validation, schema/index work, checkpointing, verification,
hashing and promotion. Those costs remain; no claim of eliminating all O(N) work.

Tests cover content/layers, absent endpoints, duplicate identities, invalid vectors,
oversized chunks, failures after nodes/links, lost fence, interruption, unproved
commit reports, low native budgets and no retry after a commit attempt. A real
candidate test interrupts the second chunk after a durable prefix and confirms
that the live primary bytes and active pointer remain unchanged.

Final grouped regression: **57 passed in 175.55 seconds**, zero failures. This
includes 20 batch tests plus the existing Global providers, recovery worker
extensions, vector/global operations and index tests. The initial 51-test slice
and subsequent focused runs overlap this final group and are not extra counts.
Ruff F checks and whitespace validation passed. JUnit receipt:
`C:/Users/jpamb/AppData/Local/Temp/pulse-recovery-batches-final-20260909.xml`.

## Remaining opportunities

1. Transfer sink: reuse native executemany while retaining the per-record identity
   and endpoint proofs currently obtained through RETURN. This is separate from
   the fresh Global candidate optimization.
2. Board rebuild workers: identify homogeneous groups without changing artifact
   acknowledgement, cancellation/resumption or saga boundaries.

No backfill or real board consolidation is part of this development test. This
wave is not installed into the local Pulse automatically; the installed artifact
receipt from the earlier checkpoint remains a distinct baseline.
