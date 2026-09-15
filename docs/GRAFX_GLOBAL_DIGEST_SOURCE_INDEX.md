# Global digest source index

Global schema bootstrap installs `pulse_global_digest_source`, a native exact
hash index on `DecisionDigest(board_id, original_node_id)`. This applies both
to fresh databases and to existing databases whose tables were already complete.
Activation backfills existing rows in a dedicated native write transaction.

The index avoids a complete digest-table scan for each source-identity probe.
The upsert still checks **all** matching source identities and separately checks
the requested primary key, within the same write transaction. Duplicate source
identities are not hidden by `LIMIT`, deduplication, or an adapter cache.

Bootstrap validates the incumbent table, column order, hash layout, exact
visibility, column-key derivation, active generation and non-stale state. An
incompatible same-named index is refused, never silently replaced. Native bucket
sizing is retained, and a legitimate later rehash does not invalidate the policy.

The mutation fence is checked before index DDL and immediately before commit.
Initial activation is proportional to the existing data and can take longer
than a normal upsert; the caller must maintain its writer lease throughout that
operation. A lost fence rolls back the activation. Repeated successful bootstrap
does not rebuild the index or advance the publication LSN.

## Vector certification after unrelated DDL

Native vector built-through LSNs are table-local. Creating this secondary index
may advance the database publication LSN without rewriting an unrelated HNSW
index. Certification therefore reports the actual native built-through LSN;
it does not replace it with the database publication LSN.

A lower watermark alone is **not** evidence of health. Certification requires
native `verify("all")` to prove heap/index coverage, matching healthy vector and
generic index views, an integer watermark within the publication bounds, and an
unchanged publication LSN from before verification through the final metadata
inspection. Verification findings, stale state, divergent or future watermarks,
and a concurrent publication all remain fail-closed.

Public Grafx index/vector inventories are intentionally cheap observations: a
cold header has `built_through_lsn=None`, even on a healthy database. Certification
therefore calls native `read_index_status(name)` to read the active generation's
validated durable header explicitly. The header must match the vector/generic
file and definition, be non-stale and carry a valid integer watermark. Resident
watermarks, when present, must agree with it. Missing durable metadata still
fails closed. The full `verify(all)` coverage proof and unchanged publication
before/after the complete probe remain mandatory; the explicit header read is
not a substitute for either. This removes the false cold-header refusal found
in the September 7 live delivery, without fabricating or rewriting a watermark.

Focused regression coverage lives in `tests/test_grafx_global_indexes.py` and
`tests/test_grafx_global_discovery.py`: fresh/repeated/cold activation, preexisting
duplicate rows, exact `IndexSeek`, incompatible incumbents, lost mutation fences,
both identity-collision checks, and adverse vector-certification observations.

## Digest-link probes

`link_board_digest` starts its existence/count probe from the digest primary key
and follows incoming `CONTAINS_DECISION` edges. Normalization uses the same
direction for counting and deleting the target digest's incoming links. This
avoids expanding every digest linked to a board hub before filtering by digest
ID. Counts still include duplicate edges, and normalization still removes
foreign-board links to that digest without touching links of other digests.

The native regression fixture in `test_grafx_global_discovery_providers.py`
compares old/new counts on a 32-digest hub: the original traversal examines 33
rows, while the inverse traversal examines the target's three incoming rows
and seeks one digest primary key. This is an operation-count witness, not a
claimed production latency multiplier.
