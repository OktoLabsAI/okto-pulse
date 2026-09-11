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

No backfill or real board consolidation is part of this development test.

## Local installation and installed-artifact validation — 2026-09-09

Installed Community checkpoint `6b42bd9eabf9c4c41f0b3169653b6aa540e7ca96`
(Pulse 0.3.3), paired with Core 0.3.3 at `9303f9852a5d0e42037fd8565823543caca60fcb`
and Grafx 0.0.5 at `8c3f6f2a6be8c8bdcdd15113fd5f37b2b965a173`.
Both local installations were updated: Python 3.13 user-site and the isolated
uv `okto-pulse` tool. Core/Grafx were already the correct artifacts and did not
need replacement. No publication or version bump was performed.

Community wheel SHA256:
`cc71dd8022a9979ff7c96eb31c64bb981dd41194fbe9febbfcc69f9f718b6459`.
Artifacts and reproducible runner:
`D:/Projetos/Techridy/okto_grafx/.grafx-tmp/pulse-batch-6b42bd9/`.

- An isolated candidate installation passed **21 tests in 94.06 seconds**:
  all 20 batch tests and authenticated recovery/cutover/idempotency. The runner
  disables source-activating conftest and proves that product imports come from
  installed site-packages, not repository source. JUnit: `installed-tests.xml`
  in the artifact directory.
- Both actual local installations independently passed app construction, complete
  native Settings catalog coverage, reader isolation during an uncommitted write,
  visibility after durable commit and reader-lease release.
- Both also passed a disposable three-digest full recovery, cold verification,
  generation promotion, exact node/link counts, vector retrieval and idempotent
  replay. Observed times for that recovery/search/replay sequence were 15.60 s
  (user-site) and 11.17 s (uv). These tiny smoke timings are **not** a controlled
  performance comparison or a real-board throughput estimate.
- Byte parity checks before/after execution matched all 397 Community, 825 Core
  and 186 Grafx package files to the exact wheels in both installations.
- The uv environment passed dependency checks (121 packages). Preexisting
  unrelated dependency conflicts in shared Python were not modified.

The first C: test attempt failed with `device_full` (13 passed, one failure,
seven errors); the independent smoke also refused an allocation. C: had only
about 0.38 GB free. That failed JUnit is retained as
`installed-tests-device-full.xml`. Repeating the unchanged product tests with
temporary databases on D: passed; no quotas/timeouts or product code were relaxed,
and no user files were removed. Free disk space before running production on C:.

This validates installed libraries/adapters, not browser UI or the running server
lifespan. No workers, backfill or actual board consolidation were started, and the
default Pulse data home was not used. Pulse remains stopped (no 8100/8101 listener).
