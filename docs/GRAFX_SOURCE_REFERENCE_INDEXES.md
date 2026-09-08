# Exact source-reference indexes: staged integration

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

## Implemented helper, not yet connected to bootstrap

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

## Required integration before deployment

1. Reconcile the schema-evolution index certification with explicit known optional
   access paths. It currently requires an exact base inventory; do not bypass this
   check or broadly accept arbitrary extra indexes. Known ordered/source families
   need their own definition and coverage validation before exclusion from the
   base count. Missing base indexes and malformed extras must still fail.
2. Connect source-index ensure to the fenced Community bootstrap before a fresh
   BoardMeta stamp; prove restart/idempotence and existing-board convergence.
3. Run the accumulated schema/index integration slice and deploy together with
   the pending bounded two-hop output change. Record source identities and live
   read-only verification; do not consume any of the 21 reserved specs.

Pulse PID 31060 does not load or create these indexes. No production DDL, graph
write, consolidation, redrive, rebuild, reset, wheel install or PyPI publication
was performed for this proof. Startup/admission memory and additional index-write
cost must be recorded when integrated; the observed seek reduction is not yet
a demonstrated end-to-end write speedup.
