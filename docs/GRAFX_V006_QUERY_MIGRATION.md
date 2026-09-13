# Grafx 0.0.6 query-language migration

The subsequent [advanced-capability adoption](GRAFX_ADVANCED_ADOPTION_0_0_6.md)
tracks native composed reads and the fixed next increments. It has its own
qualification; the older acceptance receipts below do not certify that work.

The September 12 follow-up documents [native result values and updating
subqueries](GRAFX_NATIVE_RESULT_VALUES.md): qualified node/relationship/path
observations, temporal serialization and fenced CALL/UNION writes. The historical
acceptance counts below are not evidence for that later native-value change.

## September 13 operational error contracts

`GET /api/v1/kg/schema?board_id=<routed-board>` now runs blocking schema access
off the event loop. Authorization still precedes access. Missing graph capability,
unavailable routing or corruption return typed HTTP 503 problem responses instead
of an unhandled 500. Invalid queries remain 400; bounded memory-pressure responses
retain `Retry-After`. Omitting a board does not create a route or return an invented
empty schema: integrations should pass the board whose schema they need.

Direct spec creation requires the existing explicit `delivery_context` contract.
Its neutral code-traceability failures now return HTTP 409 with the existing code
and structured details, matching the other Community source endpoints. No default
delivery context or admission bypass was added. Existing lineage errors retain
their own contracts. These changes are Community-only; Core remains agnostic.

The current affected regression passes 121 source tests and 158 installed-package
tests (overlapping, not additive). A disposable real Pulse instance also exercised
source create/update/delete, Settings catalog, schema, semantic search, API/MCP and
recovery after an actual post-WAL-barrier writer exit. The next normal Pulse writer
recovers the durable unpublished COMMIT without restarting Pulse; an isolated
read alone need not force replay/publication. Native and public logical edge-table
names differ by Community's declared mapping, not by data content. Full Grafx
regression and comparative qualification remain separate delivery requirements.

## Coordinated consumer update

Community now requires `okto-grafx[accel]==0.0.6`. Upgrade it together with the
paired Core source changes; do not run the changed queries against Grafx 0.0.5.
This work does not install or restart the global Pulse application, rebuild any
graph, redrive a queue, or start cognitive consolidation.

The active source pair is `okto-pulse-v003-kg-load-codex` (Community) and
`okto-pulse-core-kg5-codex` (Core). Pre-existing changes in both worktrees remain
separate from this migration.

## Breaking semantics and affected callers

Grafx list indexing is zero-based, negative positions count from the end, and an
out-of-range position returns NULL. `split` preserves empty tokens. Core's
`events/handlers/cancellation_decay.py::_source_owner_match_clause` and
`kg/canonical_stale_reconciler.py::enumerate_stale_sweep_page` now use the neutral
`split` spelling and positions `[0]` / `[1]` for source type / owner ID. This
preserves child-source ownership, card aliases and malformed-reference filtering.

These are provider-neutral query changes, not Grafx imports or a backend branch
in Core. Grafx-specific execution, connection lanes and transaction composition
remain Community responsibilities. No new Settings toggle or persistent-data
migration is needed, and there is no switch for the previous indexing policy.

Other new language policies include case-sensitive map keys, lazy CASE/COALESCE,
exact integer SUM, NULL-aware list/map equality and composed query scopes. The
Grafx `docs/CYPHER_COMPATIBILITY.md` and `docs/COMPOSABLE_QUERIES.md` define the
supported surface and explicit exclusions; this is not full Cypher conformance.

UNION branches must now expose the same column names in the same order. For
example, replace `RETURN n.id UNION ... RETURN m.id` with
`RETURN n.id AS id UNION ... RETURN m.id AS id`. The immutable historical Pulse
corpus retains the old mismatched-name probe and now records its explicit analysis
refusal; its source commits and query text were not rewritten. Existing supported
Pulse templates remain covered by the actual consumer integration tests.

## Validation and local build

`tests/test_grafx_query_semantics_migration.py` executes the actual Core query
builders against a real temporary Grafx database. It covers owner IDs, child
sources, malformed references and keyset pagination. The adjacent executor,
key-decisions, reflective-query, read-lane, transaction, relationship, related
context and replacement-atomicity tests are part of the consumer regression.

Final acceptance on the paired source: **191 integration tests passed**, with no
failures/skips (116.10 s). The four migration tests also passed against installed
Grafx/Core wheels in an isolated Python 3.13.1 environment. Grafx's final complete
suite passed **17,174 tests, zero failures/errors and 19 attributed skips**.
The paired Grafx repository records receipts and wheel hashes in
`docs/reports/V006_QUERY_LANGUAGE_ROUND.md`; this is local development acceptance,
not a global Pulse installation or a claim that the full legacy Core harness ran.

The follow-up [integration regression](GRAFX_V006_INTEGRATION_REGRESSION.md)
removes the shared Core bootstrap's eager dependency on a deleted adapter and
initializes schema through the neutral port, with an explicit temporary `DATA_DIR`.
It passes 81 Core tests with normal conftest, including real Community/Grafx natural
search, and adds actual cancellation/restoration writes and native recovery-fence
coverage. The earlier 23-test `--noconftest` result is historical, not the current
bootstrap acceptance. Other historical Core modules still directly import removed
test helpers, so neither result constitutes a full Core-suite pass.

### Published Grafx dependency — September 13, 2026

Community pins `okto-grafx[accel]==0.0.6`. The lock now resolves Grafx from PyPI,
with the published wheel/sdist SHA-256 hashes and its `numpy`, `google-crc32c`
and `tzdata` dependencies. It no longer requires a locally built Grafx wheel.
The `accel` extra remains explicit; NumPy is also a base dependency of this release.

Core 0.3.3 is still a paired local wheel, not a published PyPI release. Build that
Core wheel into `.grafx-tmp/language-wheel` before reproducing the paired lock.
Validate without upgrading unrelated packages:

```powershell
uv lock --check --find-links ../okto_grafx/.grafx-tmp/language-wheel
```

This does not publish Core or reinstall the active Pulse. The earlier integration
receipts used local development wheels; they remain historical evidence. Test
installed wheels in isolation and verify module paths before claiming installed
acceptance. A future Core release must separately resolve its published artifact.
