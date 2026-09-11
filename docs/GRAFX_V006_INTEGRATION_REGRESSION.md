# Pulse / Grafx 0.0.6 integration regression

Date: September 10–11, 2026. Paired sources: Community
`okto-pulse-v003-kg-load-codex`, Core `okto-pulse-core-kg5-codex` and Grafx
`feature/v0.0.6`. This is local development validation, not a release or global
installation. See [the coordinated migration](GRAFX_V006_QUERY_MIGRATION.md).

## Consumer changes and boundary

- Community requires `okto-grafx[accel]==0.0.6`; the local lock resolves the paired
  Core/Grafx wheels, not an unpublished PyPI version.
- Core cancellation and canonical stale enumeration use provider-neutral `split`
  and zero-based `[0]` / `[1]`. Card aliases, child references and invalid empty
  owners retain their intended behavior. No Grafx import, feature flag, driver
  handle or backend conditional was introduced into production Core.
- No new Settings field, storage migration or query-compatibility switch is
  necessary. Native engine features do not automatically become authorized in
  Pulse's user-query policy: its read-only and bounded-query rules remain intact.
- Existing node-removal callers use `DETACH DELETE`; plain relationship deletion
  remains valid. No production caller was found relying on orphan-producing
  node `DELETE`. UNION projections require matching aliases as documented in
  the migration; historical corpus queries were not rewritten to hide refusals.

## Regressions found and repaired

1. Shared Core test bootstrap eagerly imported the removed Ladybug bootstrap.
   `tests/conftest.py` now requests schema initialization through the neutral
   `graph_schema_manager` port. It sets an explicit temporary `DATA_DIR` before
   configuration so real adapters cannot resolve the user's default directory.
2. Two natural-query integration tests still seeded Ladybug directly. They now
   compose real Community routed providers, initialize an isolated Grafx graph,
   seed through a fenced `GraphTransaction` and complete its durability lifecycle.
   They verify both an empty query response and exact-title retrieval of a Bug
   without a vector hit. The surrounding policy tests explicitly use contract
   fakes; these must not be counted as native storage tests.
3. The logical-transfer orphan test depended on the former unsafe node `DELETE`.
   It now asserts that connected-node deletion is refused, then explicitly injects
   a missing endpoint in the physical scan. The exporter must still fail closed
   and remove its temporary endpoint map. Successful export and scan-error
   cleanup remain covered.
4. Four recovery-authority cases imported a removed test factory and exercised
   the old file/WAL layout. They now exercise actual Grafx databases, certified
   generation manifests and pointer publication. They prove preservation of live
   bytes on authority loss, no publication before the cutover fence, reconciliation
   of an already-published generation, and retention/refusal of an unpublished
   same-attempt candidate followed by an independent successor epoch.
   A fence lost after the completed journal still refuses unauthorized final
   readback; a subsequent authorized reconciliation recovers the durable success.
   Tests do **not** bypass the fence to report success or delete unresolved evidence.

Additional actual-handler regression executes cancellation and restoration through
the Community Grafx transaction adapter. It verifies child/card owner selection,
score clamping, preservation/restoration of the original score, timestamps,
idempotent retries and unchanged unrelated nodes. This small test uses an authority
double; the broader recovery/transaction suites separately exercise authority.

## Evidence

Final KG integration regression: **passed**, with no failures/errors/skips in
either broad backend batch or the focused checks below. No global deployment is
implied. Results:

| Surface | Result | Receipt under Grafx `.grafx-tmp/` |
|---|---|---|
| All Community `*grafx*.py` adapter/integration tests | 881 passed; 833.53 s | `pulse006-all-grafx-final.xml` |
| Routed graph, global recovery, cognitive/outbox persistence, API contracts and related workflows | 694 passed; 672.91 s | `pulse006-workflows-final.xml` |
| Core query policy, query contract, asynchronous cancellation/I/O, stale sweep and cancellation timestamps | 81 passed, no failures/skips; 47.21 s | `pulse006-core-final.xml` |
| Core logical-transfer architecture boundary | 30 passed, no failures/skips; 2.64 s | `pulse006-core-boundary.xml` |
| Migrated recovery authority, logical transfer and actual source lifecycle queries | 17 passed, no failures/skips; 45.75 s | `pulse006-recovery-transfer.xml` |
| KG and Settings React components, including global search, pagination/diagnostics, recovery, DLQ and queue state | 252 passed in 22 files; 74.37 s | `pulse006-frontend.xml` |
| Installed Community/Core/Grafx wheels: actual owner queries, stale pagination and cancellation/restoration writes | 6 passed, no failures/skips; 12.57 s | `pulse006-installed-wheels.xml` |

The first broad pass found five failures (one logical-transfer fixture and four
obsolete recovery fixtures), with 878 and 690 other tests passing respectively.
The receipts retain those failures; focused correction results are not substituted
for a claim that the initial batches were green.

The final broad batches total 1,575 tests without repeating files between those
two selections. Focused reruns and installed-wheel checks overlap their cases;
do not add them as unique test coverage. The native batch emits one existing
`record_property`/JUnit-format warning; it does not skip or fail a test.
Ruff on touched Python files, `git diff --check`, Grafx's documentation validator
and `uv lock --check --offline --find-links` against the paired wheel directory
also passed.

Coverage includes native readers/writers, rollback/replacement atomicity, pools,
schema bootstrap/evolution, text/vector retrieval, key decisions, graph traversal,
logical export, global recovery, route/fence control, outbox/cognitive persistence
and API contracts. Route/component tests may use explicit collaborator doubles;
the native adapter suites and migrated integration cases use real temporary Grafx.
React tests use jsdom and mocked HTTP, not a browser connected to the production
server. Existing React `act` and missing-canvas diagnostics are not native-graph
failures and remain a frontend test-harness limitation.

This is a broad **KG integration regression**, not the entire Core/Community test
inventory. Other historical Core modules still directly import removed backend
test helpers; no deleted production adapter was resurrected to satisfy them.
The prior complete Grafx run remains 17,174 passed, zero failures/errors and 19
attributed skips; this follow-up does not change the Grafx engine.

Community wheel SHA-256:
`7e16b5a27ac61640bf0120a43f74fb6c2988005f8dfd75b08ea86d5d2a62c6de`.
It was built with `python -m build --wheel --no-isolation` and installed with
`--no-deps` in Grafx's `.grafx-tmp/language-venv313`, alongside the Grafx/Core wheels
recorded in the preceding acceptance report. All three imported module paths were
asserted to reside under this isolated environment. The six tests ran with
`--noconftest`, no source `PYTHONPATH` and no checkout-selection environment
variables, so test bootstrap could not replace the installed packages with sources.
This packaging check does not establish an independently resolved clean environment
for every transitive dependency; the test venv has system-site-packages enabled.

## Reproduction

Use Python 3.13.1 and the paired checkouts above. In Community:

```powershell
$env:OKTO_PULSE_CORE_REPO='D:/Projetos/Techridy/okto-pulse-core-kg5-codex'
$env:OKTO_PULSE_COMMUNITY_REPO='D:/Projetos/Techridy/okto-pulse-v003-kg-load-codex'
$env:PYTHONPATH='D:/Projetos/Techridy/okto_grafx/src;D:/Projetos/Techridy/okto_grafx/.grafx-tmp/v006-optional-test-deps'
$native = @(Get-ChildItem tests -File -Filter '*grafx*.py' | ForEach-Object FullName)
python -m pytest -q --tb=short @native --junitxml=../okto_grafx/.grafx-tmp/pulse006-all-grafx-final.xml
$flows = @(Get-ChildItem tests -File -Filter 'test*.py' | Where-Object {
    $_.Name -match '^(test_(routed_|global_discovery_|relationship_layout_|materialization_health_|reflective_query_|card8_|card5_board_source|kg_routes|startup_graph|legacy_rebuild|queue_health|kg_cognitive_source|runtime_settings_snapshot|cypher_statement_policy|native_runtime_budget|code_traceability_kg_rebuild|c5_list_routes))' -and $_.Name -notmatch 'grafx|installed_e2e'
} | ForEach-Object FullName)
python -m pytest -q --tb=short @flows --junitxml=../okto_grafx/.grafx-tmp/pulse006-workflows-final.xml
```

In Core, using the same environment, run `python -m pytest -q --tb=short` with
`tests/test_kg_query_contract_v1.py`, `tests/test_kg_tier_power.py`,
`tests/test_async_graph_io_gate.py`, `tests/test_kg_graph_io_cancellation.py`,
`tests/test_card8_stale_sweep.py` and `tests/test_cancellation_projection_utc.py`.
Normal conftest is enabled. In Community `frontend`, run
`npm test -- --maxWorkers=2 src/components/knowledge src/components/layout/GrafxAdvancedSettings.test.tsx src/components/layout/RuntimeSettingsPanel.test.tsx`.

No production rebuild, redrive, cognitive consolidation, default data-directory
mutation, global install/restart, commit or push is part of this validation.
