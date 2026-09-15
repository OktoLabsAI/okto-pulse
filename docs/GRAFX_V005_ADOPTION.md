# Grafx 0.0.5 adoption in Pulse 0.3.3

Next coordinated query update: [Grafx 0.0.6 migration](GRAFX_V006_QUERY_MIGRATION.md).

2026-09-09. First implementation checkpoint; the broader adoption assessment is
not complete. No graph rebuild, cognitive consolidation or data reset is part of
this checkpoint.

## Architecture agreement

### September 10: default acceleration and UI attribution

The latest local Grafx 0.0.5 revision installs NumPy and google-crc32c as base
dependencies (`[accel]` remains compatible). Community consumes the native catalog:
`codec` and `vector_math` now show `numpy` by default in Menu > Settings.
Existing explicit `pure`/`auto` overrides remain unchanged; changing constructor
options requires a runtime restart. NumPy vector arithmetic retains its documented
tolerance; page encoding remains byte-identical. No database migration is required.

Settings, About and the Knowledge Graph header now show **Powered by Okto Grafx**
using the supplied, bundled SVG. Attribution stays visible while graph content is
loading or unavailable, and uses one shared accessible component with fixed icon
dimensions. No external image request or Core/provider coupling is introduced.

Validation: 43 backend Settings/persistence tests and 67 frontend tests passed
(attribution, NumPy defaults, preserved overrides, Settings, Header and KG
diagnostics). TypeScript/Vite build and packaged frontend synchronization passed.
The SVG is an exact copy of the supplied asset (SHA256
`a7fda36cc11dd6c29e0883b83b062e34b0d57222fd69b6e494bb26f789ea2aaa`).
Build/install evidence lives in the paired Grafx checkout's
`.grafx-tmp/accel-default/`; source completion does not mean an already-running
Pulse process has loaded these changes.

The built wheels also passed a clean staged-install smoke: exact package bytes,
base dependency metadata, complete Settings catalog, independent reader visibility,
durable writes, recovery/cold verification, generation promotion, vector search,
idempotency and reopen. All 120 staged dependencies passed `uv pip check`. The
recovery portion completed in 23.94 s on disposable data; this is smoke evidence,
not a production performance benchmark.

After explicit stop/update/restart approval, both local entry-point environments
(Python 3.13 user install and the uv tool environment) received these exact wheels:

- Grafx 0.0.5: SHA256 `4f09d7e3ba1b8c7b716aa0b278bea2f39428db68fea71b27f56521f3b4bffcc5`.
- Pulse 0.3.3: SHA256 `2f3004ddbffabf986b255770c6dd1c7d9268f70727a2b7fab0dbbb84ac52a2f7`.

Installed-file parity, NumPy catalog defaults and retained pure overrides passed
in both environments; all 120 uv tool dependencies passed `uv pip check`. Pulse
was restarted with explicit `DATA_DIR=C:/Users/jpamb/.okto-pulse`. No database reset,
manual rebuild or configuration-override rewrite was performed.
Post-restart verification: PID 4892 owns both 8100 and 8101, frontend HTTP 200,
and the served `/assets/index-Dvt1-ZTD.js` contains both Grafx attribution and the
bundled SVG. Startup logs confirmed the Grafx board route without an error traceback.

### Adapter boundary

Core may evolve, but its contracts must remain implementable by other databases.
Keep native types, index names, constructor options, physical IDs and provider
dispatch in Community. Do not encode the full Grafx API in nominally neutral DTOs.

For future optional search/analytics capabilities:

- Specify an independently meaningful semantic contract, not a native method copy.
- Distinguish provider support from readiness for a particular board/index/scope.
- Default undeclared optional support to unavailable. Existing `GraphCapabilities`
  and `GraphCapabilityUnavailable` are precedents, not a reason to claim unimplemented
  capabilities today.
- Permit another adapter to decline optional capabilities explicitly. Use fallback
  only when it preserves the requested semantics and budget; otherwise report
  unavailable. Unsupported search is not a successful empty result.
- Never hide corruption, expired authority, timeout or partial execution behind
  unsupported capability/fallback. Keep these failures distinguishable.
- Do not substitute token search for substring or native ranked retrieval for
  Pulse's cross-type expansion/recency/reranking pipeline.
- Multiwriter support alone does not authorize concurrent application sagas.
  The generic per-board consolidation coordinator remains unchanged until conflict
  sets and SQLite/outbox/domain invariants justify a neutral policy revision.

## Implemented in this checkpoint

1. Dependency pin: `okto-grafx[accel]==0.0.5`. The candidate is the exact wheel
   built from Grafx `8c3f6f2`, SHA-256
   `5395fdd8d9619342c0c35e952b5b1422f810ef18c978a0d39ad3614d6718f529`.
2. Complete native `DatabaseConfig` Settings inventory for this candidate. Added
   help/validation exposure for four formerly missing memory/cache options.
   The existing inventory test compares every native dataclass field to the UI
   policy and help, and tests forwarding into writer and reader constructors.
3. Configurable, bounded Board read participants, applied at process startup.
4. Vector search now owns the operation-scoped read lane through indexed search,
   exact fallback, result shaping and snapshot cleanup. Standalone resolver-only
   construction remains supported. No query/ranking/fallback semantics changed.

## Settings / API

All entries are Community-owned. The existing Settings tooltip portal remains
above the modal; no inline expanding help is introduced.

| Setting | Default | Meaning / tradeoff |
| --- | --- | --- |
| `kg_grafx_read_participants` | `2` | Integer 1–8, independent lazy read handles per Board. More handles permit overlapping snapshot reads but multiply caches/descriptors. Does not configure Global readers, bound the task queue, change writer authority or bypass per-board commit coordination. |
| `kg_grafx_options.vector_hnsw_memory_budget_bytes` | `null` | Optional positive logical memory cap per derived HNSW picture. Low values can refuse searches; not process RSS. |
| `kg_grafx_options.vector_hnsw_total_memory_budget_bytes` | `null` | Optional positive aggregate HNSW logical cap per handle, including construction and retired pictures still held by readers. Not a budget shared between handles. |
| `kg_grafx_options.index_key_cache_pages` | `64` | Integer 0–65536, retained key-memo pages per index. Zero disables retention, not execution. |
| `kg_grafx_options.index_key_cache_bytes` | `1048576` | Integer 0–2147483648, retained logical key-memo bytes per index. Either cache cap at zero disables retention. |

These values require a restart, not a graph rebuild. Existing generation geometry
is unchanged. Cache capacities are not preallocated, but a resident Board may
retain `(1 + kg_grafx_read_participants)` independent allowances. Global adds its
own handles. Settings are not a total-process RAM limiter.

`GET /api/v1/settings/runtime` adds `kg_grafx_read_participants` (default 2 for
older clients) and includes the four native options in `grafx_settings_catalog`.
`PUT` accepts the reader count as a strict JSON integer; invalid values return
422. It persists desired configuration while effective values remain unchanged
until restart; `desired_values` and `restart_required` retain their existing
contract. `KG_GRAFX_READ_PARTICIPANTS=4` configures the environment equivalent.
Native options remain inside the validated `kg_grafx_options` object.

## Baseline correction

The previous static assessment compared selected installed Core files to the
pagination worktree. Full import testing showed that it is not a sufficient pair
for this Community source: it lacks `GraphInvalidQuery`. The matching existing
Core worktree is `okto-pulse-core-kg5-codex` at `9303f98`. This checkpoint does not
modify Core implementation; the selected Core already contains the neutral error
contract and earlier query/consolidation improvements. Never silently remove the
Community error mapping to accommodate an older installed Core.

## Validation

- 265 passed: settings catalog/configuration/persistence/constructor forwarding,
  vector semantics and scope cleanup, read lanes, graph store, pool, memory budget
  and Cypher executor. Includes a real Grafx test that holds the writer participant
  section while an independent reader completes.
- 30 passed: routed Global runtime/composition and runtime-settings snapshots.
- 55 passed: Core commit coordination, cancellation-safe blocking I/O, public
  contracts and Discovery card result semantics.
- 20 passed: Settings panel and advanced settings UI, including portal tooltips,
  reader count persistence, zero cache retention and memory controls.

These are focused/regression slices, not a new full Pulse regression or a live
UI benchmark. The earlier 102-test pass is a subset, not an additional count.

Packaged frontend build/sync passed. The matching Core/Community wheels were
installed into both user-site and uv tool environments, and both passed package
byte parity, application construction, complete Settings catalog and a disposable
native read-isolation/committed-visibility smoke. No workers or real-data operations
were started. The uv tool environment passed dependency checks; unrelated packages
in the shared Python user-site have conflicts and were left unchanged.

The local lockfile now points to the validated candidate directories under the
sibling Grafx `.grafx-tmp` folder. This is an explicit local-development lock, not
a portable public release: keep the candidate artifacts available and regenerate
against published versions when preparing a release.

## Remaining adoption work (not claimed delivered)

Pre-commit closure: recovery-only and release validators now require 0.0.5,
matching pyproject/lock. Their focused contract suite passed 256 tests, with nine
deselected (full isolated release/launcher runs and marked E2E/slow/stress cases
were not repeated). An initial run exposed inherited backend environment values
and missing explicit artifact inputs for the long gates; the focused run clears
only its process environment and does not change operator configuration. The
previously installed wheels predated this validator-only alignment. They have
since been replaced by checkpoint `6b42bd9`; see the
[installed-artifact validation receipt](GRAFX_RECOVERY_BATCHING.md#local-installation-and-installed-artifact-validation--2026-09-09)
for the new package identity, recovery tests and local disk-space warning.

1. Global Discovery read participants with lifecycle drain and generation/privacy
   protection; do not simply remove the shared lock.
2. Remaining blocking-dispatch paths and admission/backpressure by workload.
3. Opt-in native full-text search and neutral support/readiness contracts.
4. Vector fallback attribution and specialized hybrid retrieval without semantic loss.
5. Homogeneous bulk writes and a proven neutral same-board concurrency policy.
   The first, Community-only [Global candidate batch implementation](GRAFX_RECOVERY_BATCHING.md)
   follows checkpoint `5e13e45`; transfer/Board worker adoption remains pending.
6. Bounded analytics/projections, efficient transport and operational provenance.

Use bounded, fixed workloads and grouped regressions; do not consume all reserved
specs or introduce open-ended performance gates.
