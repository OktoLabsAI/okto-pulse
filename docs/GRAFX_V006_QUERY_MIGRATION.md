# Grafx 0.0.6 query-language migration

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

Before PyPI release, build wheels from the paired Core checkout and Grafx checkout
into Grafx's `.grafx-tmp/language-wheel`, then resolve Community with:

```powershell
uv lock --offline --find-links ../okto_grafx/.grafx-tmp/language-wheel --upgrade-package okto-grafx --upgrade-package okto-pulse-core
```

The current lock uses those local wheel sources, not an unpublished PyPI artifact.
They must exist to reproduce the local integration build. Test the installed
wheels in an isolated environment and verify module paths before claiming wheel
acceptance. Release packaging must re-resolve the published artifacts separately.
