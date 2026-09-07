# Write-path integration milestone — 2026-09-07

This source slice collects the already live Global delivery corrections and
validates them independently of unrelated worktree changes. It does not launch
cognitive consolidation, redrive, rebuild or consume the 21 reserved specs.

## Exact source identity and digest links

Global bootstrap installs the native composite exact index described in
`GRAFX_GLOBAL_DIGEST_SOURCE_INDEX.md`. Both source-identity and primary-key
collision checks remain in one write snapshot. Duplicate source matches are
not hidden. Activation backfills existing data, is fenced, and is idempotent.

Digest-specific link probes start at the digest primary key and inspect its
incoming links, not every digest of the Board hub. Duplicate counts, foreign
ownership refusals and normalization semantics remain intact.

Invalid-link reconciliation compares the complete expected-ID set with a full
aggregated inventory in one write snapshot before staging any deletion. Only
proven-invalid positive IDs are deleted in batches of 512, with a single fenced
commit. Independently chunking NOT IN would delete valid links and is explicitly
not used. Result/memory budget failures abort rather than truncate the inventory.
A second-batch refusal rolls back the first batch too. This removes the oversized
parameter blocker; inventory enumeration itself is still proportional to links.

Core's separate, backend-neutral visibility slice bounds assignment payloads to
512 IDs and skips already-correct values. NULL is explicitly materialized. These
assignments commit separately: failure propagates, prevents normal delivery
completion, and retry converges the already-applied prefix. They are not claimed
to have the single-transaction atomicity of invalid-link deletion. The native
integration fixture covers 2414 IDs, both visibility values, NULLs, no-op retry
and isolation from another board.

Vector certification after unrelated index DDL still requires native verify(all)
coverage and stable publication around the whole probe. The durable header's
actual table-local watermark is checked against both public views and publication
bounds, not fabricated as the current global LSN. Cold, stale, missing, divergent
and concurrent-publication observations are covered by refusal tests.

## Endpoint-query review: corrected regression, not a fabricated gain

The transaction adapter now shares logical/physical relationship translation
with the read executor. Native query evaluation remains in Grafx, and Core stays
backend-agnostic. Explicit PK bindings are retained for pair creation and lineage
endpoints. A native differential test shows the current engine already seeks
twice for the legacy WHERE-based pair creation as well: zero scanned rows for
both spellings with 8 and 32 nodes. This syntax change is not a new speedup.

The pending edge_exists rewrite was different: inline endpoint maps followed by
WHERE r.rule_id placed the relationship predicate before endpoint equalities in
the conservative planner. Its native fixture scanned 9 rows with 8 nodes plus
one staged edge. Restoring leading endpoint equalities followed by the rule
predicate restores the source seek and at most one examined edge in the fixture.
This restores the existing committed query form and fixes a worktree/runtime
regression; it is not a gain over the previous source HEAD. Planner predicate
ordering and refusal semantics were not relaxed.

Tests also exercise owner-visible staged edges, absent endpoints, mismatching
rule IDs, rollback and native verify(all). The full transaction, lineage,
active-set and orchestrator suites cover the surrounding write/fence contracts.

## Deployment boundaries

The isolated Community candidate passed 198 tests in 502.66 s, covering all ten
listed transaction/Global/relationship suites. Core's isolated 19-test slice
passed in 26.71 s and was committed as `c4a01bf`. Ruff and whitespace checks
passed. Earlier failed candidate runs exposed the edge_exists regression above;
the 198-test result is for its corrected source, not those rejected candidates.

Validation uses isolated exports of the exact Core and Community Git indexes,
plus current Grafx source. UI/settings persistence and deterministic repair
changes remain outside this milestone. No wheel, PyPI release, main merge or
global installation is implied. The live process needs an orderly restart to
load the edge_exists restoration; previously live delivery fixes must not be
replayed merely because their source milestone is now recorded.
