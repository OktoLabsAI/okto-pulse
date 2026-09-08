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

## Deployment and honest end-to-end boundary

Community commit `3e95746` was pushed to the integration branch. Pulse PID
7896 shut down gracefully (one board closed, zero failures); PID 2816 now
source-loads the correction with Pulse 0.3.3 and Grafx 0.0.4. This is not a
new globally installed wheel or a PyPI release.

The first UI load after restart remained poor: graph HTTP 200 in 41.276 s,
census HTTP 200 in 60.643 s. The observer's 60-second response wait expired;
the original request completed normally and was not restarted or duplicated.
Recorded phases: schema 17.639 s, stats nodes 12.887 s, node counts 7.527 s,
edge counts 22.473 s; subgraph nodes 30.577 s and edges 10.486 s. These cold,
overlapping phases are not an isolated before/after comparison with the warm
profile, and do not demonstrate a global UI improvement.

The UI displayed 500 / 2779 nodes. Pagination subsequently added 500 unique
nodes and 897 edges, HTTP 200, 69 layouts considered / 65 scanned / 4 skipped /
zero failures; UI displayed 1000 / 2779. The combined pagination-and-pending
verification took 4.156 s (not the graph request alone). REST still reports
21 pending, zero in progress, 19 consolidated; ledger hash unchanged.

Full cold-load latency remains unresolved. The next profiling target is the
remaining overlapping native admission/endpoint-resolution and Health source
work, not disabling Health, suppressing validation, or consuming benchmark specs.

## Audited latest-head capability (2026-09-07)

A trace aligned with the first KG opening on PID 1792 identified the full
`kg_cognitive_source_revisions` ORM read (2.374 s elapsed, 1.156 s thread CPU).
The simultaneous 35-second GIL-only capture had 977 samples and zero errors.
Among JSON-encoding leaves in the source-diagnostic Health worker, 93 traversed
`latest_cognitive_source_records` and 64 traversed historical DTO construction.
Samples are not wall-time percentages; pool admission and native reads also
overlap. This supports removing duplicate history work, not attributing all
cold-load latency to this adapter.

Core now defines optional, backend-neutral `LatestVerifiedCognitiveSourceReader`.
`_cognitive_durable_digest` selects it when available, otherwise retains the
full enumeration contract. Capability failures propagate without fallback.
Community's `enumerate_latest_verified` reads the same complete scoped ledger,
normalizes raw row mappings, and uses the unchanged Core latest-row validator
to check every canonical fingerprint and duplicate revision before selecting
heads. Only selected heads become DTOs. The digest consumer still revalidates
those DTOs, including mutable nested payloads. Full `enumerate` stays unchanged
for consumers that need history. The legacy missing-revision-table read remains.

No stored hash is trusted, no historical row is skipped, and no cross-call
cache is introduced. Empty/None/wrong revision digests and superseded corruption
still fail. No schema, append, graph transaction, authority or persistence
change is involved. Core remains agnostic to Grafx and SQLAlchemy.

This remains O(history) validation and ORM hydration, not an O(heads) read.
For N historical records and H heads, it removes N-H DTO constructions and
canonical serializations from enumeration plus latest selection. With this
board's N=5424 and H=290, that is another 5134 redundant serializations removed,
separate from the earlier decoder correction above.

Read-only live-source comparison (`mode=ro&uri=true`), in execution order:

| Path | DTOs returned | Enumeration (s) | Including Core digest (s) |
| --- | ---: | ---: | ---: |
| Full history | 5424 | 7.247 | 11.876 |
| Audited latest | 290 | 6.968 | 7.561 |
| Audited latest | 290 | 6.634 | 7.255 |
| Full history | 5424 | 5.792 | 9.695 |

All four final digests equal
`8a39bd1c2b4e364880fa9e6794b82aed635748fca42ba1be9e2a76c85c4b6b2a`,
count 290. OS cache and concurrent tests are uncontrolled; this is output parity
and a structural work reduction, not a controlled global/UI speedup claim.

Core port/rebuild-source tests: 107 passed in 33.46 s. Initial Community adapter
run: 42 passed, one test NameError caused by an incorrect test edit (not a runtime
failure); corrected and all five affected cases passed in 11.78 s after reboot.
The slices overlap and are not a single distinct combined count. Coverage
includes digest/order parity, generations/board scope, complete history, legacy
table absence, missing/bad hashes, divergent duplicate revisions, post-return
payload mutation, exact hash work counts and no unsafe fallback.

Windows rebooted at 2026-09-07 21:19:53 local time; PID 1792 and service listeners
were absent afterward. No cause is inferred. The reserved-spec ledger stayed
byte-identical. Restart/deployment validation is recorded separately below.
