# Native Grafx results in Pulse Community

September 12, 2026; paired source qualification against the Grafx 0.0.6
development checkout. This is not an installed/global Pulse upgrade.

## Boundary and permissions

Whole-pattern Grafx MERGE now works through the existing fenced transaction port:
unbound endpoints and fixed multi-hop paths match completely or create every
unbound entity, never silently reusing a partial match. The new
`tests/test_grafx_general_merge.py` covers commit/rollback, repeat-input identity,
JSON paths/endpoints, independent readers, late native failure and lost fences.
The read-only UI executor still rejects MERGE as `unsafe_cypher` before database
resolution. No Core implementation or Settings change is required.

This follow-up is also exercised against three privately installed wheels, with
package-origin and source/wheel/installed-byte assertions. Exact artifacts,
dependency-reuse caveats and receipt hashes are recorded in the Grafx repository's
`docs/reports/GENERAL_MERGE_PULSE_QUALIFICATION.md`. This is Python adapter/JSON
qualification, not live HTTP/browser acceptance or a global upgrade.

`community/adapters/grafx_query_values.py` uses only Grafx's public value classes
and formatters. Both the read-only executor and `GraphTransaction.execute`
normalize native observations into plain Python/JSON-compatible values. Core
has no native driver import, backend branch or Grafx-specific result class.

Native writing `CALL` and `UNION` are available through the fenced transaction
port, not through the user read-only endpoint. Branches and invocations share one
unit of work: an independent reader does not see private writes; a late native
statement failure removes all effects of that statement, preserving earlier
successful statements. Losing the fence refuses before branch execution. No
independent inner commit, retry of ambiguous writes or new permission is added.

## Result contract

Nodes expose flattened properties plus `_LABEL`, `_LABELS`, `_ID`, `_PROVENANCE`
and `_PROPERTIES`. An unlabeled node has `_LABEL: null` and `_LABELS: []`.
`_PROPERTIES` preserves every actual property, including names colliding with
reserved metadata keys. Relationships have the same property/identity/provenance
fields and qualified `_SRC` / `_DST` endpoint IDs. For nodes, `_LABEL` is the
first canonical logical label (or null), and `_LABELS` is the complete logical
membership. Label changes do not change the physical owner in `_ID.table` or
duplicate node identity. For relationships, `_LABEL` is the native relationship
type. Neither value is guessed from a property.

Paths expose `_NODES` and `_RELS` as ordered lists. Other expression tuples remain
tuples in Python and naturally become JSON arrays. Nested values receive the same
conversion; these observations do not retain a database or transaction handle.

The `_ID` object now has **five fields**, replacing the old table/offset-only
shape. Consumers must not interpret it as a business primary key or write handle:

| Field | Meaning |
| --- | --- |
| `database` | Database UUID as hexadecimal text |
| `table` | Decimal-string table identity |
| `kind` | `node` or `relationship` |
| `offset` | Decimal-string record incarnation, or null for a provisional ID |
| `provisional` | Transaction-private nonce as hexadecimal text, or null |

Exactly one of `offset`/`provisional` is populated. Keeping wide identities as
strings avoids JavaScript rounding. `_PROVENANCE` carries `read_lsn`,
`schema_version`, `version_lsn` and `pending`; an allocated record ID alone does
not prove that a write committed. The payload is observational data and cannot
be passed back as native entity authority for SET/DELETE.

DATE, LOCALTIME, TIME, LOCALDATETIME, DATETIME and DURATION are rendered using
their native ISO formatters, retaining nanoseconds, extended years and recorded
offset/zone semantics. They are strings, not host `datetime` objects or inferred
typed parameters. Legacy TIMESTAMP retains its UTC microsecond text format within
Python's calendar range. Outside that range its exact int64 microseconds are
returned as `{"type":"timestamp","micros":"..."}`, avoiding overflow and
floating-point rounding. Vectors remain lists.

Additional result observations use the following plain JSON representations:

| Native value | Pulse result representation |
| --- | --- |
| DECIMAL(p,s) | `{"type":"decimal","coefficient":"12500","precision":12,"scale":4}` for 1.2500; no conversion through float or loss of scale |
| Expression NaN | `{"type":"float","value":"NaN"}`; defensive normalization also names Infinity/-Infinity rather than emitting invalid JSON numbers |
| BLOB | `{"type":"bytes","hex":"00ff"}`; preserves every byte |
| UUID | Canonical hyphenated string |
| Map with any non-string key | `{"type":"map","entries":[[1,"number"],["1","text"]]}`; entries preserve key/value pairs without string-key collisions |

String-keyed maps remain ordinary JSON objects, and all conversions recurse into
entities, paths and nested collections. These are **observational output forms**,
not a tagged input decoder or a universal round-trip serialization protocol:
a user map that resembles a tag remains user data and grants no authority.
Do not infer native parameter types from their shape. Native nonfinite storage
remains prohibited, including nested properties; the result adapter grants no
new arithmetic, storage or write permission. There is no new Settings option.

## Validation and scope

`test_grafx_native_result_values.py` checks detached nodes/relationships/paths,
property-name collisions, qualified and wide IDs, pending observations, six
temporal families and nested JSON, refusal of map-based entity authority, and
heterogeneous unlabeled nodes after reopen. `test_grafx_updating_subqueries.py`
checks committed/rolled-back unit and returning branches, correlated invocation
order, independent-reader isolation, late failures and read-only/fence refusal.
The existing path tests are retained with the expanded ID contract.

The projection-cleanup test now seeds a finite edge and supplies an invalid NaN
preservation receipt. It still proves refusal before deletion. NaN in expressions
does not authorize NaN/Infinity in stored properties; no storage check is bypassed.

The paired Grafx repository records final receipts in
`docs/reports/FP4_PULSE_SOURCE_QUALIFICATION.md`. This source qualification does
not replace remaining frozen-profile, installed-wheel, API/browser or full-Pulse
acceptance. It does not claim that every possible query scalar is accepted by
every Pulse HTTP/MCP schema.

## Qualified physical transfer

The Grafx logical-transfer adapter resolves catalog definitions with
`table(name, kind="node"|"rel")` and forwards that kind to `scan_rows_v1`.
Its schema validation compares the full `(kind, name)` inventory. An unexpected
relationship cannot hide behind the spelling of a declared node table; mismatch
refuses before export. Endpoint mapping remains node-qualified and disk-backed,
with the same batch bounds and cleanup on failed scans or missing endpoints.
This also supports a physical relation table sharing a node's name without
confusing rows or table-local record IDs.

The Pulse logical artifact describes one declared business type per node, not a
general label-set graph. The source accepts implicit membership or the explicit
singleton matching that declared type. Empty, renamed or additional memberships
raise `LogicalSchemaError` during the complete snapshot preflight, before exposing
an export; temporary endpoint maps and the read transaction are closed on refusal.
A held canonical snapshot remains valid across a later label mutation, while a
new snapshot detects the changed membership. This prevents silent label loss; it
does not add arbitrary label sets to Pulse's portable artifact. Grafx's own native
format-4 transfer separately preserves full/empty memberships.

No Core contract or Settings option changes. Same-name transfer, complete
board/global physical round trips, failure cleanup and stale/malformed source
refusals are covered by the adapter/physical-matrix tests; exact source-only
receipts are recorded in the paired Grafx report above. This is not an installed
runtime, HTTP/browser or production-rebuild qualification.

## Logical relationship reads and installed UI qualification

Validated read-only queries using recognized fixed patterns can name logical
relationships without endpoint labels, for example
`MATCH (a)-[r:depends_on]->(b) RETURN count(*) AS total`. Community resolves an
unproven endpoint pair to all declared physical type alternatives; Grafx performs
the native matching, filtering and aggregation. Proven unique pairs still use
one physical table. Both the standalone executor and scoped transaction use this
translation; writes retain their existing endpoint proof and fence requirements.

The translator does not newly support backtick names, variable-length syntax or
caller-written type-alternative lists. These shapes require physical names when
supported by the native query contract. A custom narrowed transaction layout must
provide its physical table resolver if it does not store logical table names.
No Core dependency, public result envelope or Settings option changes.

Canonical-only HTTP still refuses anonymous endpoint patterns when it cannot
enforce layer isolation: name every endpoint, or request `include_working=true`
when that broader scope is intended. The read-only endpoint still refuses MERGE.

The paired Grafx report `docs/reports/PULSE_HTTP_UI_PARITY_QUALIFICATION.md`
records 153 source tests, installed artifact provenance, eight real HTTP checks,
and actual browser pagination from 500 of 510 to all 510 nodes. This is an isolated
synthetic fixture, not a production rebuild or complete Pulse regression.
