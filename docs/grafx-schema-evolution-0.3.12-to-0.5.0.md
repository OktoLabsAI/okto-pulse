# Grafx schema evolution: Pulse 0.3.12 to 0.5.0

## Status, policy and bounded scope

This document freezes the final additive-schema unit of M-PULSE-3. The unit
accepts exactly the released predecessor projection described below and rebuilds
it logically into a separate, fresh Grafx generation carrying the already
published M-PULSE-3C manifest.

This is an out-of-place rebuild, not an in-place physical upgrade. That decision
is required by `EVOLUTION_PLAN_CODEX.md` sections 9.3.6 and 9.5: a physical
format change uses export/import or a new generation, and the previous schema
must have a logical migrator plus an `n-1/n` fixture. Accordingly:

- Grafx `CATALOG_FORMAT_VERSION` remains 1;
- heap rows, catalog encoding, `TableDef`, indexes and the public Grafx package
  surface do not change;
- raw `ALTER`, a generic migration framework and arbitrary schema deltas remain
  unsupported;
- the source database is never mutated;
- the candidate is never bound or activated by this unit.

Cross-backend Kuzu/Ladybug export/import remains M-PULSE-5. Provider selection
and binding remain M-PULSE-6, and board fencing, delta capture and atomic cutover
remain M-PULSE-7. A successful result here is an unbound, cold-validated
candidate; it is not authority to serve Pulse traffic.

## Historical authority and topology

Three histories must not be conflated:

- `okto-pulse-core@24a7aa47109f125212a4ddf90035681d48c4ac51`
  records `SCHEMA_VERSION = "0.3.12"`. A direct child on the
  released/pinned lineage,
  `de1f494003d4d95af5da8bdb8af99b6f816d42d0` (tag `v0.3.2`) records
  `0.5.0` and is an ancestor of the M-PULSE Core pin
  `ab61b9a785f2018312fc91541a580877fd068bbb`. This released/pinned
  lineage owns the predecessor selected here.
- `okto-pulse-core@02418584b4716b6b6b9630ab2a082658087a2344`
  records `0.4.0` and is an ancestor of
  `715ad68193cb14263c02f08fd4ff9ff7921b9648`, whose schema is
  `0.5.0`, on a separate development lineage. Neither commit is an
  ancestor of the baseline pin. That intermediate is retained as historical
  evidence but is not an admitted production source.
- The Grafx manifest and Grafx `BoardMeta` bootstrap first exist in Pulse
  Community commit `7e126a7130090c00891f8d1d35bd44819afe7a7a`.
  No deployed historical Grafx catalog or Grafx `BoardMeta` fixture before
  `0.5.0` is claimed.

The source is therefore an explicit synthetic Grafx projection. Node-column
order comes from the Community DDL at `fa9ab58`, logical relationships come
from Core `24a7aa4`, and `BoardMeta`, physical relationship names and the
one-space-per-node representation follow the M-PULSE-3C Grafx projection.
Tests must keep that attribution explicit.

## Frozen source, target and delta

The canonical Core diff `24a7aa4 -> de1f494` is strictly additive. Every one
of the 11 node tables gains these nullable `STRING` properties, in this
order, immediately before `embedding`:

1. `investigation_receipt_id`
2. `source_ref`
3. `attestor_actor_id`
4. `declared_revision`
5. `workspace_state_id`
6. `code_path`
7. `symbol_qualified_name`
8. `symbol_kind`
9. `selector_kind`
10. `selector_fingerprint`
11. `resolution_state`

The exact new physical relationship endpoint tables are:

1. `precedes__Entity__Entity`
2. `supports__Entity__Requirement`
3. `supports__Entity__Constraint`
4. `supports__Entity__Criterion`
5. `supports__Entity__APIContract`
6. `supports__Entity__Decision`
7. `supports__Entity__TestScenario`
8. `supports__Entity__Entity`
9. `derives_from__Entity__Entity`
10. `overlaps__Entity__Entity`

No existing table, column or space is removed. No existing name, type,
nullability, primary key, endpoint pair, relationship property, vector-space
definition or logical order changes.

| Contract | Nodes | BoardMeta | Physical relationships | Spaces | Logical relationships | Logical fingerprint |
|---|---:|---:|---:|---:|---:|---|
| synthetic `0.3.12` source | 11 x 33 columns | 1 x 5 columns | 59 | 11 | 13 | `f4f9905b1012b98df6669117c0ab8feb926f763d7d4c26caf91cc0f138354717` |
| current `0.5.0` target | 11 x 44 columns | 1 x 5 columns | 69 | 11 | 16 | `4a7b425bf4b8c4864be633c1a87f034e5f7f641019dc029015b7d3ca786deb81` |

The predecessor descriptor is the current descriptor with the 11 properties
above, logical `precedes`, `supports`, `overlaps`, and only the
`Entity -> Entity` endpoint of `derives_from` removed, and with
`schema_version = "0.3.12"`. Canonical schema JSON uses UTF-8,
`ensure_ascii=true`, sorted keys and separators `(',', ':')` before
SHA-256. A source must contain every other expected object and no unexpected
object.

## Public Community surface

The bounded adapter surface is exported from
`okto_pulse.community.adapters.grafx_schema_evolution`:

```python
rebuild_grafx_schema_candidate(
    source: okto_grafx.Database,
    candidate_path: str | os.PathLike[str],
    *,
    batch_size: int = 256,
) -> GrafxSchemaCandidateResult
```

`candidate_path` must name a durable generation; `":memory:"` is refused.
The function owns every candidate handle it opens and returns with the
candidate closed. It derives `page_size` and `partitions_per_table` from
`source.identity`, so cold reopen uses the same physical identity contract.
It never closes, checkpoints, recovers or writes the caller-owned source.

Before its first candidate open, the adapter resolves an absolute canonical
candidate path with platform case normalization, derives one sibling lock path
from that canonical value, acquires a non-blocking kernel-backed `FileLock`,
and holds it through the final candidate close. Lexical aliases such as
`x/../y` and platform-equivalent case aliases must contend on the same lock.
The candidate path is a private capability: it is not in any Pulse binding and
is not exposed to another sanctioned opener while the lock is held. Contention
is a typed refusal. This is the verifiable single-builder fence for
M-PULSE-3; it does not pretend to be the board-wide traffic fence owned by
M-PULSE-7.

`batch_size` is an exact integer in the inclusive range `1..1024`.
It bounds the row count of each materialized page and candidate write
transaction, not source sort memory or transaction bytes. The existing Grafx
result/intermediate row budgets and transaction/WAL byte budgets remain
authoritative and can refuse an operation. It is not a throughput guarantee.
This unit is serial because `QueryResult` is materialized and a Grafx
transaction is not promised thread-safe. Parallel export/import and a
cursor/bulk API remain later performance work.

`GrafxSchemaCandidateResult` is a frozen, slotted dataclass with these exact
fields in this order:

```python
source_schema_version: str
target_schema_version: str
source_schema_fingerprint: str
target_schema_fingerprint: str
source_snapshot_lsn: int
logical_data_fingerprint: str
node_row_counts: tuple[tuple[str, int], ...]
relationship_row_counts: tuple[tuple[str, int], ...]
candidate_database_uuid: bytes
changed: bool
```

The count tuples follow manifest order. `changed` is false only for an already
completed candidate that is logically identical to the fresh source snapshot
observed by this call.

The result is created only after terminal cold validation. Neither
`BoardMeta.schema_version = "0.5.0"` nor a target-shaped catalog alone is a
completion receipt.

## Source and candidate state machine

All arguments and the complete source catalog are validated before
`candidate_path` is opened. The source must be open, must have the exact
synthetic predecessor catalog, and its one `BoardMeta` row, read inside the
export transaction, must contain:

- a non-empty `board_id`;
- `schema_version = "0.3.12"`;
- a valid `bootstrapped_at`;
- either both embedding metadata values absent or a non-empty model with
  dimension 384.

Before any writable candidate open, source and candidate paths are normalized
to absolute canonical, platform-case-normalized values. When both are durable,
neither may equal, contain or be contained by the other. Existing paths also
use the platform's physical-identity/`samefile` check. The canonical
candidate value is the sole input to the sibling lock name.

A nonexistent or truly empty candidate directory may then be created. An
existing non-empty candidate is first opened observationally with
`read_only=True`; its UUID must differ from the source before any writable
open or recovery is permitted. If that observational open requires mutation,
cannot establish identity, or finds a non-Grafx artifact, the adapter refuses
the path without a writable retry. An ambiguous commit may be reconciled
writably only inside the same invocation that already established the
candidate UUID before the ambiguity. No alias or overlapping tree of the
source can become the target.

Only these candidate states are recognized:

| Candidate state | Result |
|---|---|
| Empty Grafx catalog | Initialize and build this candidate. |
| Exact current catalog, one exact current `BoardMeta`, and data equal to the normalized source snapshot | Cold-validate and return `changed=False`; write no candidate database page, WAL record or LSN. |
| Exact current catalog with the reserved build marker | Refuse as an abandoned partial attempt. Use a new generation; do not resume or upsert. |
| Any partial, mixed, extra, malformed, unknown-version or divergent state | Refuse before copying; never repair in place. |

Initialization creates all 11 current spaces, 81 current tables and exactly one
`BoardMeta` row in one transaction. That row preserves the source
`board_id`, `bootstrapped_at`, `embedding_model` and
`embedding_dimension`, but carries the private exact marker
`"building:0.3.12->0.5.0"`.

Schema and marker must commit together. Therefore the existing M-PULSE-3C
bootstrap sees an incomplete attempt as a version mismatch and cannot
accidentally stamp it current. The sanctioned candidate API holds the
candidate-path lock, and no provider/graph-transaction binding receives this
private path in M-PULSE-3. A failed initialization is either empty or marked
after recovery, never silently current.

## One source snapshot and bounded copy

After source catalog preflight, the adapter opens exactly one source read
transaction. All metadata, node pages and relationship pages come from that
fixed snapshot. The source catalog is recaptured immediately after opening the
snapshot and after the last source page; any drift from the exact predecessor
catalog invalidates the candidate.

The source read transaction remains open until its canonical fingerprint and
all counts are final. Concurrent DML after the snapshot is deliberately not
merged: this candidate represents exactly the reported snapshot LSN. A
board-wide fence and the final delta/cutover protocol belong to M-PULSE-7.

`ORDER BY` in the current Grafx engine materializes its complete operator
input before applying `SKIP/LIMIT`. Consequently, `batch_size` bounds only
returned rows and candidate row intents: it does not bound source scan/sort
memory or intermediate rows. Offset pagination repeats full scans and sorts;
the existing result/intermediate row budgets can refuse the read, and
transaction/WAL byte budgets can refuse a candidate write. Such a refusal is
typed and abandons the marked candidate without source mutation. A streaming
cursor/bulk protocol is deliberately deferred to M-PULSE-5 rather than added
to this schema gate.

### Nodes

Each source node table is read in manifest order with all 33 predecessor
columns explicitly projected, ordered by the logical primary key and paged with
`SKIP/LIMIT` inside the one snapshot. Each candidate row is created once with
all 44 target columns:

- all 33 predecessor values keep their exact logical type and value;
- the 11 introduced properties are present and `NULL`;
- `VectorValue.space_ref` is never copied. A non-null vector is rebuilt from
  its values and dtype using the target space resolved by manifest name;
- the row's logical primary key is unchanged.

Candidate writes use bounded transactions and exact affected-row assertions.
There is no `MERGE`, upsert, deduplication or partial retry. A duplicate key,
missing value, wrong type, wrong vector dimension/space or affected-row
mismatch invalidates the entire candidate generation.

### Relationships

Only the 59 predecessor physical relationship tables are exported. For each
table, the query explicitly projects source-node primary key, target-node
primary key and every relationship property. Rows are ordered by those
canonical values and paged with `SKIP/LIMIT` inside the same source snapshot.

Import resolves both endpoints by their logical primary keys and creates
exactly one relationship for every exported row. Direction, self-loops,
properties, `NULL` values and repeated identical or distinct parallel edges
are preserved. No `DISTINCT`, set conversion or map keyed only by endpoints
is allowed. The 10 newly introduced physical relationship tables must remain
empty.

## Canonical logical data fingerprint

Source and candidate are compared as normalized logical multisets, not as
physical files. The internal hash domain is the frozen text
`pulse-grafx-schema-rebuild/1`; it is not the portable M-PULSE-5 artifact.
The exact `M3/v1` codec is:

- values are JSON arrays: `["null"]`, `["bool", true|false]`,
  `["int64", "<canonical decimal>"]`, `["float64", "<float.hex()>"]`,
  `["string", "<value>"]`, `["timestamp_us", "<canonical decimal>"]`, or
  `["vector", "<expected-space-name>", "<dtype>", ["<float.hex()>", ...]]`;
- integers have no leading plus or zero padding except the value `"0"`;
  timestamp micros use the same decimal rule; doubles and vector components
  must be finite and use Python's lowercase `float.hex()`, preserving
  negative zero;
- a metadata record is
  `["meta", board_id, target_version, bootstrapped_at, model, dimension]`,
  with every value encoded by the rule above;
- a node record is
  `["node", table_name, [[column_name, encoded_value], ...]]`, with all 44
  columns in target-manifest order;
- a relationship record is
  `["rel", logical_type, from_type, to_type, encoded_from_pk,
  encoded_to_pk, [[property_name, encoded_value], ...]]`, with properties in
  manifest order;
- each record is JSON-encoded with UTF-8, `ensure_ascii=true`,
  `sort_keys=true` and separators `(',', ':')`, with no trailing newline;
- stream order is the one metadata record; node tables in manifest order with
  records sorted by their canonical encoded primary-key value; then logical
  endpoint entries in the frozen manifest order with their records sorted
  lexicographically by complete canonical record bytes. Equal relationship
  records remain repeated;
- SHA-256 receives `b"pulse-grafx-schema-rebuild/1\0"`, followed for every
  record by its byte length as unsigned 64-bit big-endian and then its bytes.
  The published digest is lowercase hexadecimal.

The golden codec vector is these four records, including the repeated edge:

```json
["meta",["string","board-\u03b1"],["string","0.5.0"],["timestamp_us","1"],["null"],["null"]]
["node","Entity",[["id",["string","e1"]],["flag",["bool",true]],["count",["int64","-2"]],["score",["float64","0x1.0000000000000p-1"]],["when",["timestamp_us","7"]],["embedding",["vector","entity_embedding","float64",["0x1.0000000000000p+0","-0x0.0p+0"]]],["missing",["null"]]]]
["rel","supports","Entity","Requirement",["string","e1"],["string","r1"],[["confidence",["float64","0x1.8000000000000p-1"]],["created_at",["timestamp_us","9"]],["note",["null"]]]]
["rel","supports","Entity","Requirement",["string","e1"],["string","r1"],[["confidence",["float64","0x1.8000000000000p-1"]],["created_at",["timestamp_us","9"]],["note",["null"]]]]
```

Its exact digest is
`9d1123371dc1ed6009737f24bdf19a2a293fa7598585c3e45ba598ff64b6b175`.
The production canonical stream contains:

1. the `BoardMeta` payload with schema version normalized to `0.5.0`;
2. every node record keyed by table and logical primary key, with all 44 target
   columns in manifest order;
3. every relationship occurrence keyed by the logical
   `(relationship_type, from_type, to_type)` triple, endpoint logical keys
   and all properties in manifest order.

The source normalization injects the 11 new fields as present `NULL`.
Relationship duplicates are emitted repeatedly. Vectors include the expected
space name, dtype and ordered components, never numeric `space_ref`.

M-PULSE-3B's frozen codec is bijective between that logical triple and the
physical relationship table. The physical table name is validated against the
codec during export/import but is not an independent data-fingerprint key.

The fingerprint excludes physical database UUID, table/space numeric ids,
`RecordId`/`RecordRef`, page locations, MVCC history/CSNs, WAL bytes and
HNSW topology. Those are correctly rebuilt in a fresh generation and cannot be
claimed as preserved. Catalog fingerprints, counts, vector definitions,
logical vector payloads and index verification remain separate mandatory
checks.

## Terminal certification without a publication race

After every copy batch is durable, the candidate must pass, in order:

1. exact current catalog fingerprint and shape;
2. exact per-table counts, with all 10 new relationship tables empty;
3. normalized logical multiset/fingerprint equality with the source snapshot;
4. exact vector-space and index definitions plus payload coverage;
5. `checkpoint()` and `verify("all")` with no findings.

The final stamp is a separate candidate write transaction while the
candidate-path lock is still exclusively held. Inside that same transaction,
the adapter rescans the complete candidate and recomputes the normalized
fingerprint and counts. Only an exact match may execute a conditional update
from the one reserved marker to `"0.5.0"`. The conditional update must return
exactly the one expected `board_id`.

Grafx read-only scans do not currently promise to register the complete scan as
an OCC read set. The design therefore does not claim that the terminal
transaction alone fences another writer: single-writer authority comes from
the private unbound path plus the held kernel lock. An unsanctioned opener that
obtains the private path and ignores its lock violates this unit's explicit
precondition and invalidates any result; excluding that actor is not presented
as an engine-enforced fence. M-PULSE-7 must revalidate before publication.

After the durable stamp, the adapter checkpoints, closes the candidate, reopens
it read-only, and repeats exact catalog, `BoardMeta`, counts, normalized
fingerprint, vector/index coverage and `verify("all")`. It closes that handle
before returning the result.

A failure or ambiguous commit during the stamp is reconciled by cold reopen:

- the reserved marker means the generation is abandoned;
- `0.5.0` is accepted only after the complete cold proof above;
- any other state is divergent and abandoned.

The adapter never treats `BoardMeta = 0.5.0` alone as success. Another process
can modify a closed unbound candidate after return, so M-PULSE-7 must revalidate
the eventual immutable receipt, database UUID and binding/fence revision at its
own publish boundary.

## Failure, recovery and idempotency

Any exception before a candidate batch commit rolls that batch back. Any
failure after one or more candidate commits leaves a generation whose build
marker makes it unusable by normal bootstrap/provider paths. The caller must
allocate a new generation; this unit never deletes or resumes the old one.

If a candidate commit outcome is ambiguous, cold reopen/recovery may reveal
either the prior durable prefix or the next durable prefix. Both remain marked
and abandoned. The migrator itself issues no source write, checkpoint or
recovery operation. With a quiescent source, its catalog, durable data/WAL/LSN
and main/index files remain byte-invariant; with admitted concurrent source
writers, the proof is the fixed snapshot plus zero source mutation attributable
to the migrator. Reader-registration, lock/control and telemetry artifacts are
not misrepresented as database mutations.

Calling the function again with an already completed candidate is a zero-write
no-op only after a fresh source snapshot and the full cold proof establish the
same logical fingerprint, metadata and counts. Only a source change visible in
those logical authorities is refused. A source that advanced and later returned
to the identical logical state is intentionally indistinguishable without a
persisted M-PULSE-5/7 receipt; the result reports the snapshot LSN observed by
this call. No source or candidate binding changes in any path.

Adapter failures use the existing Pulse `GraphError` taxonomy through the
Grafx error mapper and include bounded, non-secret details: backend, operation,
phase, reason and table when applicable. Raw engine exceptions must not cross
the Community boundary.

## Fixed acceptance gate

The gate is finite. Discoveries owned by M-PULSE-5, 6 or 7 are recorded there
without widening this unit.

1. **Policy and authority:** assert sections 9.3.6/9.5 compliance; exact source
   and target commits, versions, descriptors, fingerprints, counts, ordered 11
   columns, ordered 10 relationship tables and the separate `0.4.0` lineage.
2. **Predecessor manifest:** golden-test 11 x 33 nodes, BoardMeta x 5, 59
   physical/13 logical relationships and 11 spaces; refuse every missing,
   extra, reordered or malformed object before candidate creation.
3. **Public boundary:** validate source lifecycle, durable candidate path,
   identity inequality, exact integer batch bounds, frozen result semantics,
   typed errors and absence of new Grafx-core exports/format changes.
4. **Populated fixture:** include every scalar type, `NULL`, empty/non-empty
   text, timestamps, all node tables, nullable and non-null embeddings, all 59
   relationship tables, both directions where declared, self-loops, and
   identical/distinct parallel edges.
5. **Logical preservation:** prove exact per-table counts and canonical
   multiset/fingerprint equality; 121 introduced node properties are present
   `NULL`; the 10 new relationship tables are empty; vector payload/dtype and
   target space-name mappings are exact.
6. **Marker safety:** schema plus build marker is atomic; the exact
   `ensure_current_grafx_board_schema` M-PULSE-3C bootstrap refuses the marker;
   the candidate API owns the private path and lock; every
   partial/mixed/unknown candidate refuses and is never resumed.
7. **Snapshot and concurrency:** all source pages use one snapshot; source
   catalog drift refuses; canonical lexical/case path aliases and two sanctioned
   builders for the same candidate contend on the kernel lock; the terminal
   rescan plus marker CAS shares one write transaction under that lock.
8. **Fault/crash matrix:** inject before/after initialization, every node and
   edge page, checkpoint, verify, terminal rescan, stamp commit and cold reopen.
   Recovery yields empty, marked-abandoned or fully cold-proven current, never
   a partial candidate accepted as current.
9. **Source invariance:** for a quiescent fixture, compare source catalog,
   logical data, main data/index files, WAL bytes and LSN before/after, excluding
   documented reader/control artifacts. A concurrent-writer fixture instead
   proves one fixed snapshot and zero source writes by the migrator. The source
   remains reopenable and verified.
10. **Idempotency and limits:** a completed logically identical candidate is
    zero-write; logical fingerprint/metadata/count drift refuses; abandoned
    attempts require a new path; returned-page and candidate-transaction row
    counts stay within the validated `batch_size`, while source sort cost and
    existing Grafx query row and transaction/WAL byte budgets remain
    authoritative.
11. **Cold integrity:** exact catalog/metadata/data/index checks and
    `verify("all")` pass after read-only reopen; the returned UUID and
    fingerprint match the reopened candidate.
12. **Regression:** M-PULSE-3A/B/C, fresh bootstrap, query compatibility, raw
    `ALTER` refusal, public API, Ruff, formatter and diff checks remain green;
    Grafx catalog/heap/API snapshots and `CATALOG_FORMAT_VERSION = 1` are
    unchanged. One explicit assertion compares fresh bootstrap, repeated
    bootstrap and predecessor rebuild and requires all three to equal the same
    target schema fingerprint.

## Explicit non-goals

- no in-place row/catalog/index evolution;
- no public Grafx schema mutation API or catalog v2;
- no Kuzu/Ladybug reader or portable migration artifact;
- no generic version graph, transform registry, resume or repair;
- no provider activation, router/binding write, directory rename or cutover;
- no board-wide fence, delta capture, rollback journal or retention policy;
- no ANN topology/physical-id equality promise;
- no parallel copy or new cursor/bulk protocol.

Those are not hidden debts in M-PULSE-3. They remain owned by the already
defined later Pulse milestones or the post-Pulse roadmaps.
