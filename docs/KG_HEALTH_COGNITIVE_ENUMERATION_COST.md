# KG Health: cognitive history validation cost

## Evidence and scope (2026-09-07)

A 40-second, 50-Hz stack-only `py-spy` capture of live Pulse PID 7896
recorded 1992 samples without sampling errors. A normal KG Refresh returned
Health in 2.532 s, the graph in 5.768 s, and census in 24.944 s (HTTP 200).
Health response completion does not mean its background probes have finished.

Stack classification attributed 1257 samples to Health, 244 to relationship
census, 58 to graph edge loading, and 433 to other work. These are sampled
stack counts, not additive wall-time or CPU percentages. Within Health,
466 leaf samples were JSON encoding; 654 stacks traversed rebuild-source
diagnostics, and 590 traversed source enumeration. This establishes substantial
overlapping source-audit work, not that it explains all native graph latency.

Read-only SQLite aggregates found 290 cognitive base rows (2,816,456 payload
characters) and 5134 revision rows (50,453,717 payload characters). Enumeration
must continue checking all historical revisions: selecting only latest rows
would conceal corruption that the existing contract rejects.

## Implemented: compute each revision fingerprint once in the adapter

`sqlalchemy_kg_cognitive_source._revision_record` previously computed a
canonical fingerprint, compared it to storage, then passed it into
`CognitiveSourceRecord`, which recomputed that same fingerprint immediately.
It now constructs the DTO from fresh decoded payload/evidence, letting Core
compute the fingerprint, and compares that computed value with storage before
returning the record. Empty/missing stored fingerprints still fail explicitly.

This removes one canonical serialization/hash per revision (5134 on this
board) without a cache, trusted stored digest, skipped history, schema change,
changed fingerprint format, or Grafx-specific Core dependency. Append/write
logic and subsequent Core latest-revision validation are unchanged. Invalid
payloads still fail; valid payloads with wrong stored digests retain
`cognitive_source_fingerprint_mismatch` and its remediation.

## Read-only parity experiment

A private diagnostic opened SQLite with `mode=ro&uri=true`. The comparison
arm reconstructed the redundant hash immediately before the optimized decoder.
All four enumerations returned 5424 records and the same final 290-record
digest: `8a39bd1c2b4e364880fa9e6794b82aed635748fca42ba1be9e2a76c85c4b6b2a`.

| Arm, execution order | Enumeration (s) | Including Core digest (s) |
| --- | ---: | ---: |
| Duplicate | 12.036 | 16.353 |
| Single | 6.784 | 10.303 |
| Single | 6.310 | 9.481 |
| Duplicate | 7.576 | 10.712 |

Other focused tests were running during parts of the experiment; warm-up and
contention prevent treating these four timings as a controlled speedup claim.
The deterministic claim is removal of 5134 duplicate serializations with exact
output parity. No end-to-end UI improvement is claimed from these timings.

Focused tests verify one canonical hash per returned revision, complete history,
fresh payload revalidation after tampering, corruption in superseded and latest
revisions, and missing/wrong stored digests. A first test fixture attempted to
persist an empty digest and was correctly rejected by the SQLite length CHECK;
the decoder boundary test now exercises that malformed detached row directly,
without disabling database constraints.

Validation: 8 focused decoder/enumeration tests passed in 30.89 s, then the
accumulated adapter + revision-schema suite passed 47 tests in 170.05 s. The
unchanged Core port + rebuild-source suite passed 104 tests in 35.90 s. These
slices overlap; their counts are not a single distinct combined total. Ruff
and the scoped whitespace check passed.

No consolidation, redrive, rebuild, graph reset or historical-debt cleanup was
performed. Reserved cognitive ledger SHA256 remains
`4AFF1AB6EE6C6E621C6598148154A04298500B8DA92EBF0F5E90AD081B2217F4`.
