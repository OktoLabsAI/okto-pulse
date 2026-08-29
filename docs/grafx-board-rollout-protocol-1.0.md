# Grafx Board rollout protocol 1.0

Status: frozen implementation contract for M-PULSE-7.

This protocol migrates one Pulse Board from an existing Ladybug binding to a
separate Grafx generation.  It does not change Core ports, does not perform an
in-place physical conversion, and does not synchronously dual-write the two
engines.

## Scope

The protocol is Board-only.  Global Discovery already has its independently
governed generation/recovery protocol and is outside the per-Board canary in
M-PULSE-7.

The existing M-PULSE-5 logical schema, snapshot, fingerprint and candidate
certificate are the data contract.  The existing M-PULSE-6 binding is the
sole runtime routing authority.  A rollout never makes settings or physical
discovery a second routing authority.

## Durable authorities

For one Board, three durable authorities exist:

1. `graph_backend_binding.json` selects exactly one active backend/generation;
2. `boards/<board_id>/rollout/` records the rollout state and mutation outbox;
3. the M-PULSE-5 candidate certificate proves the logical census,
   fingerprint, schema, cold reopen and `verify()` result of a candidate.

The rollout store uses prepared-first, monotonically sequenced records.  A
record is durable before the corresponding source mutation starts.  A crash
may therefore leave an ambiguous prepared record, but cannot leave an
unrecorded mutation.  Reconciliation copies a fixed logical source snapshot
into a fresh Grafx generation and covers the complete outbox range visible at
the same frozen boundary.  Consequently an ambiguous record is resolved by
the actual source state, never by guessing whether the operation committed.

This is a logical reconciliation outbox, not the Grafx physical WAL and not
the Global Discovery projection outbox.

## State machine

The persisted states are:

- `shadowing`: Ladybug is active; fresh Grafx generations may be built and
  compared;
- `canary_ready`: a certified Grafx generation covers a recorded outbox
  high-water mark and has no unexplained divergence;
- `grafx_active_rollback_open`: CAS selected Grafx, while the unchanged
  Ladybug source remains eligible for rollback only before a Grafx write;
- `grafx_active_rollback_closed`: rollback was durably closed;
- `rolled_back`: CAS restored the unchanged Ladybug binding before a Grafx
  write was accepted;
- `completed`: rollout retention and audit were completed;
- `erased`: privacy erasure invalidated the rollout permanently.

Every transition is a compare-and-swap on the rollout version and expected
state.  A stale transition fails closed.

## Shadow cycle

1. Under the Board exclusive mutation window, authenticate the Ladybug
   binding and start the rollout outbox.
2. Still under that short window, retain one Board reader pin, open two fixed
   source snapshots at that same boundary, and read the outbox high-water
   mark.  One snapshot feeds the transfer and the other remains independent
   for result comparison after the transfer snapshot is consumed.  Both are
   consumed after releasing the window; the pin is released only after both
   closes are proven.
3. Stream the transfer snapshot into a new, empty and unbound Grafx
   generation.
4. Require checkpoint, cold reopen, `verify("all")`, schema equality, census
   equality and logical fingerprint equality.
5. Persist a checkpoint covering the captured outbox high-water mark.  A
   newer cycle may supersede an older unbound shadow only after the newer
   certificate is durable.
6. Run the frozen result corpus against both fixed views and persist either
   the comparison receipt or a divergence.  Divergence prevents canary and
   cutover.

Each source mutation surface records a canonical family/payload envelope
before invoking the active provider.  Success, rollback and ambiguous failure
are terminal audit facts; all are safely covered by a later full logical
reconciliation.

## Canary and cutover

The final outbox delta is the range after the last certified checkpoint.
Workers reconcile that range into a fresh generation using the same fixed
logical snapshot protocol.  Cutover then takes a short exclusive window and
requires all of the following to remain true:

- the active binding still has the expected Ladybug SHA-256;
- the outbox high-water equals the certified checkpoint cursor;
- there is no unresolved divergence;
- the candidate certificate still authenticates the same generation and
  fingerprint;
- a cold-opened candidate still passes `verify("all")`;
- the binding CAS succeeds against the expected Ladybug SHA-256.

Any changed cursor releases the window without switching traffic; another
shadow cycle absorbs the delta.  The binding file is atomically replaced and
the replacement directory is fsynced before the cutover reports success.

`canary_ready` is a transient, write-fenced state.  A writer cannot extend the
outbox or reach either provider while a process may be resuming between the
canary transition and binding publication.  Promotion revalidates the full
canary gate immediately before binding CAS; recovery performs the same check
before completing a candidate-bound transition.  A stale cursor, receipt,
candidate or rollout version therefore leaves traffic on Ladybug and requires
a new shadow cycle.

## Rollback rule

Ladybug is never overwritten by forward shadowing or cutover.  Immediately
after CAS, the rollout enters `grafx_active_rollback_open`, and a read-only
rollback can CAS the original unchanged Ladybug binding back while no Grafx
mutation has been accepted.

Version 0.0.1 deliberately chooses the safe branch of the frozen M-PULSE-7
rule: before the first Grafx mutation, mutation capture atomically changes the
rollout to `grafx_active_rollback_closed`; only then may the provider mutate
Grafx.  Thus no write is accepted while claiming a reverse delta that was not
applied and confirmed.  A later release may keep the window open by adding a
confirmed Grafx-to-Ladybug logical applier, without changing the binding or
Core contracts.

Failure to persist the closure refuses the Grafx write.  A crash after the
closure but before the write leaves rollback conservatively closed and data
unchanged.  A crash after the prepared record is durable is reconciled from
the active Grafx source like every other ambiguous mutation.

## Privacy erasure

Privacy erasure first enters the existing exclusive Board mutation window,
then durably changes the rollout to `erased`.  This tombstone remains present
and refuses mutation, shadow and canary work while both physical backends are
swept.  Only after both physical erasures report success is rollout storage
itself finalized.  A partial physical failure therefore remains retryable
without losing the durable invalidation.  The sweep removes and proves
absence of:

- every Grafx generation and its sidecars;
- the active or retained Ladybug source;
- rollout database, journal/WAL/SHM, manifests and temporary files;
- shadow candidates, rollback metadata, quarantine and binding artifacts.

Only the aggregate absence proof may report success.  Partial failure remains
retryable and must never recreate a binding or candidate.  A retry repeats the
physical absence checks before finalizing the rollout bytes.

While that durable tombstone exists, the explicit Board route creation doors
(`initialize`, `adopt` and `rematerialize`) fail closed before physical
discovery or publication.  Their check and route publication share the same
Board writer/mutation authority as privacy invalidation, so a partial sweep
cannot race startup and make retained Ladybug bytes routable again.  Once the
journal and both physical stores have proved absence and finalization removes
the tombstone, the same Board identifier may be initialized as a new empty
graph.

## Administrative recovery fence

Offline `recover_wal_only` is a routed administrative write.  Ladybug recovery
durably advances the rollout high-water before replay starts so that a stale
candidate cannot be promoted.  Grafx recovery durably closes an open rollback
window before a writable database is opened or replay begins.  Route
authentication for these recovery phases is inspection-only, because recovery
must be able to repair a physical database before normal opening succeeds; it
still requires the persisted binding and never falls back to settings or path
discovery.

## Frozen acceptance gate

The acceptance run uses the versioned M-PULSE-7 trace manifest.  It expands to
exactly 10,000 deterministic mutations with a fixed seed and frozen family
distribution, expected fingerprints and crash points.  It also executes three
complete close/reopen/recovery cycles, the frozen Pulse query corpus with its
30-second external limit, and publishes throughput, p50/p90/p99 plus peak
memory for Ladybug and Grafx.  The gate requires zero unexplained divergence
and zero `verify()` failure.  Gate inputs cannot change after the run starts.

Only the CLI over a clean, committed Community checkout and the exact pinned
Core and Grafx revisions can emit a certifying receipt.  Its process authority
contains a stable catalog of every tracked productive Python source in those
three checkouts, including source and recursively aggregated code-object
digests.  An import audit validates both already-resident and later lazy-loaded
modules against that catalog; runner, factory, Board/Pulse workers and both
crash/recovery children must retain the same authority digest.  Alternate
factories and injected runners remain available only in `test_only` mode and
cannot set the acceptance result to passed.
