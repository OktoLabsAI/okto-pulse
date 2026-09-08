# Exact source-reference indexes

## Evidence and scope — 2026-09-07

The previously selected cognitive-write/provenance investigation found repeated
`source_artifact_ref` equality reads without a physical access path. A separate
read-only handle against the live board reported no exact index on this column
for Entity, Decision or Bug. The existing Core active-lineage query returned one
row after scanning 953 Entity rows (0.114 s), 203 Decision rows (0.030 s), or 3 Bug
rows (0.004 s). These single observations are not whole-consolidation timings.

The Core query already begins with a seekable equality. Grafx already supports
custom exact hash indexes and revalidates each candidate against the snapshot
heap. No new engine operator, query rewrite, Core adapter dependency, fingerprint
cache or on-disk format is needed. Repeated source lookup can avoid a full table
scan; it remains proportional to matching historical versions/bucket work, not
guaranteed O(1) under arbitrary duplicate history or hash collisions.

## Additive Community policy

`grafx_source_indexes.ensure_pulse_grafx_source_indexes` defines Community-owned
`pulse_source_<node type>` exact hash indexes on `source_artifact_ref` for the
manifest's node tables. Incumbents must match name, table, columns, layout,
visibility, derivation, active state and non-automatic/non-stale status. Conflicts
are refused without replacement. Hash sizing remains the native policy rather
than a new Community tuning constant. Creation uses the caller's writer fence;
a race is accepted only after a fresh registry view proves the exact winner.
No-winner failures propagate. Repeating a completed ensure does not write WAL.

Existing active-lineage ordering, superseded filtering and generation tie-breaks
remain in the query. Index maintenance, both OCC checks, WAL, recovery and snapshot
visibility remain entirely native.

## Focused evidence

- 12 policy/real-engine tests passed in 7.83 s. A separate pinned-reader test
  passed in 5.96 s (13 distinct cases across these slices).
- On 1002 synthetic rows, baseline scan = 1002 rows; indexed scan = 0 and
  `rows_seeked` = 3. Both return the same latest active generation, not the
  higher-numbered superseded predecessor.
- Read-your-writes after a source-reference change, checkpoint/read-only reopen,
  missing lookup, complete native verification, conflicting definitions,
  pre-mutation fence refusal, exact/conflicting concurrent winner and creation
  failure are covered. A separate reader keeps its prior snapshot while another
  participant updates the indexed source; a fresh read sees the new reference.
- Initial fixture errors were corrected: schema DDL needs an explicit write
  transaction, index seek reports `rows_seeked` rather than `rows_scanned`, a
  read-only cold open requires checkpoint completeness, and public registry views
  are captured snapshots rather than live collections. No native rule was weakened.

## Integration checkpoint — 2026-09-07

The helper is now connected to the normal fenced bootstrap after ordered-page
indexes and before a fresh BoardMeta stamp. Existing boards gain missing indexes
without changing their metadata stamp; a second pass is a no-op. A source-index
fence failure leaves a fresh board unstamped and a subsequent authorized pass
converges normally. No graph rows or cognitive state are rebuilt by this step.

Schema evolution now separates a **closed** set of known optional indexes from
the required base inventory only after validating their physical definitions and
coverage: Pulse ordered-page indexes, Pulse source indexes and native automatic
RecordId indexes for the manifest's node tables. Unknown names, including unknown
suffixes under a known prefix, remain refused. Removing a base index cannot be
masked by adding a valid auxiliary with the same total inventory count.

A real-bootstrap test exposed a pre-existing certification mismatch: native
indexes use nonced physical generation files, while the old verifier expected
only `index/<logical-name>.idx`. Certification now derives the exact file through
Grafx's `index_generation_file` when the definition has a nonzero nonce and checks
the active view's matching integer nonce/state. Legacy nonce-zero files retain
their exact naming requirement. The vector facade's file must agree with its
certified underlying index. Arbitrary files and mismatched generations remain
refused. Native publication, WAL and recovery are unchanged.

Validation so far: initial combined schema/source/ordered slice had 72 passes and
the certification mismatch above; after correction 34 focused/real-bootstrap/
existing-inventory tests passed in 61.32 s. The final generation-file mutants plus
bounded-output slice passed 40 tests in 6.73 s. Counts overlap. Read-only inspection
of all 183 currently installed board indexes also passes the corrected inventory
certification. This metadata/coverage check does not substitute for `verify(all)`.

The populated schema-migration/cold-reopen/no-op regression passed in 41.60 s as
the single accumulated migration check before deployment. The current runtime has
not yet loaded source-index activation; deployment evidence follows separately.

## Deployment sequence

1. Populated schema-migration regression completed.
2. Preserve a quiescent board copy before the additive index activation.
3. Deploy together with
   the pending bounded two-hop output change. Record source identities and live
   read-only verification; do not consume any of the 21 reserved specs.

Pulse PID 31060 does not load or create these indexes. No production DDL, graph
write, consolidation, redrive, rebuild, reset, wheel install or PyPI publication
was performed for this proof. Startup/admission memory and additional index-write
cost must be recorded when integrated; the observed seek reduction is not yet
a demonstrated end-to-end write speedup.
