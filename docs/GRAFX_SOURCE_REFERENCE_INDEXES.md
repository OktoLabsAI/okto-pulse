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
of all 183 pre-activation board indexes also passes the corrected inventory
certification. This metadata/coverage check does not substitute for `verify(all)`.

The populated schema-migration/cold-reopen/no-op regression passed in 41.60 s as
the single accumulated migration check before deployment.

## Deployment completed — 2026-09-07

Pulse PID 31060 was stopped gracefully and its terminal exit, process absence and
free API/MCP ports were confirmed before replacement. A quiescent board copy was
preserved at Grafx `.grafx-tmp/pre-source-index-board-20260907`: 972 files,
197,369,632 bytes, every file SHA256-matched to the source. No files were deleted.

Pulse 0.3.3 PID 23228 now runs Community `a5a5c3c`, Core `64ff2b2` and Grafx
`425bcc4` from source. This also loads the bounded two-hop output change. It is
not a global wheel install or a PyPI release. Normal bootstrap activated all 11
source indexes. There are 11 new index files totaling 5,873,664 bytes; no buffer
budget was raised. This is additional index storage and write-maintenance work,
not a free optimization. Whole-process peak admission memory and whole-write
overhead have not been isolated.

Immediately after DDL, a raw read-only diagnostic correctly refused admission
because the WAL was not checkpoint-complete. No manual checkpoint or writable
diagnostic was used. A normal UI graph read succeeded, and subsequent native
read-only admission succeeded in 0.753 s. The Community reader admission path
already handles this specific condition using a single-flight recheck, normal
write fence and native checkpoint before retrying the lane; the observation is
consistent with automatic maintenance, without a captured trace identifying the
exact checkpoint caller. No logical consolidation or repair was replayed.

Read-only comparison against the quiescent pre-index copy:

| Source lookup | Before: rows scanned / seconds | After: candidates sought / seconds |
| --- | ---: | ---: |
| Entity | 953 / 0.1256 | 1 / 0.0384 |
| Decision | 203 / 0.0241 | 1 / 0.0042 |
| Bug | 3 / 0.0037 | 1 / 0.0042 |

Each pair returned an identical one-row result digest. These are single samples
from different physical copies/cache states, not controlled end-to-end speedups;
the demonstrated improvement is removing full-table source-reference scans.
Live inventory certification passed. Native `verify(all)` completed clean in
8.080 s: 14,850 pages, 8,739 records, zero findings.

The normal browser KG opening returned HTTP 200 for graph and stats: 500 nodes,
703 edges, zero failed edge tables; totals were 2,779 nodes and 4,424 edges.
The UI displayed the total. Browser resource times were 11.900 s for graph and
7.590 s for stats, not a controlled comparison or a global UI acceleration claim.
The cognitive ledger stayed at 21 pending, zero in progress, 19 consolidated,
zero failed/skipped, total 40, with unchanged SHA256
`4AFF1AB6EE6C6E621C6598148154A04298500B8DA92EBF0F5E90AD081B2217F4`.
No consolidation, redrive, rebuild, reset or replay of the seven prior repairs
was triggered. The remaining specs remain reserved for write benchmarks.
