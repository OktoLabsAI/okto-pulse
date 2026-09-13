# Grafx in Menu > Settings

## Grafx 0.0.6 advanced adoption update

The existing read-participant control now applies per Board **and** to Global
Discovery (default 2, range 1–8, restart required). The label/help and health
memory estimate include Global's one writer plus these independent readers.
New ranked retrieval, history activation/retention and bounded analytics are
explicit per-operation API capabilities, not constructor settings or silent
startup migrations. Their complete options, limits, permissions and one-way
activation cautions are in [Advanced adoption](GRAFX_ADVANCED_ADOPTION_0_0_6.md).
No Settings checkbox implicitly builds text indexes, enables history or deletes
retained versions.

## Pulse query-value ceiling

`max_query_value_characters` has a **default and hard maximum of 65536** in
Pulse Community. Positive integers from 1 to 65536 are accepted; null, booleans,
fractional numbers and larger limits are rejected. The limit counts characters,
not bytes, per query string parameter or result value, not per whole transaction.
Oversized values are refused, never truncated. Keeping this ceiling does not
make an oversized consolidation payload succeed or automatically redrive it.

Set it in **Menu > Settings > Grafx > Advanced Grafx settings**, in the runtime
settings API as `kg_grafx_options: {"max_query_value_characters": 65536}`, or via
`KG_GRAFX_OPTIONS='{"max_query_value_characters":65536}'`. Changes require restart;
omitting the override keeps 65536. Lower limits can reject otherwise valid work.
The catalog publishes `minimum: 1` and `maximum: 65536`; the UI prevents saving
invalid drafts and backend validation independently rejects bypass attempts.
Other catalog fields without Community bounds publish null for these metadata.

An existing persisted options object violating this ceiling is ignored as a
whole at startup with `settings.invalid_persisted_value`, using the validated
base configuration instead. The stored object is not clamped or rewritten.
Invalid environment options fail configuration validation. Reader and writer
pools validate the same policy. This is Community-owned: Core remains agnostic,
and standalone Grafx retains its wider native configuration range.

Validation evidence (2026-09-13): 126 backend tests passed across query-value
policy, settings catalog, snapshot persistence and database pools; 22 frontend
tests passed across the advanced controls and runtime settings panel. Native
boundary tests cover the default, explicit 65536 and a reduced limit, Unicode
character counting, rejected oversized writes with rollback, independent readers
and durable reopen. These tests use temporary databases, not user boards.

## Original constructor inventory

The Grafx tab now inventories all **36** `DatabaseConfig` fields of the integrated
Grafx 0.0.4. A contract test compares the catalog to the native dataclass so new
fields cannot silently disappear from the UI.

Three ordinary controls cover page size, descriptor revalidation and page-buffer
budget (MiB per handle). Advanced settings provide another 27 editable controls:
identity allocation, descriptor capacity, recovery policy, lease/commit timeouts,
reader-stall threshold, WAL/checkpoint, query/traversal/transaction/index-build
budgets, automatic index sizing, codecs/checksums and vector-search policies.
Every field has a tooltip available through hover, keyboard focus or click.
Tooltips use a fixed-position portal: they do not move the form or get clipped
by the modal's scroll container, and close on Escape, blur or scrolling.

Deployment note: the SPA index is cached in process memory at startup. After
rebuilding/synchronizing frontend assets, restart Pulse before browser validation;
refresh alone can still receive an index referencing the previous inline-tooltip
bundle. Verify the served bundle and computed `position: fixed`, not just the
source or generated files. The tooltip portal is outside the modal with z-index
100 (modal: 50), so opening help cannot resize or recenter the form.

Six native fields are visible but **managed by Pulse**, not silently omitted:

| Native field | Why it is not a general runtime setting |
|---|---|
| `path` | The route binding owns the current board/global generation. |
| `read_only` | Reader and writer lanes deliberately have different roles. |
| `partitions_per_table` | Persisted conflict geometry must remain compatible with existing generations. |
| `metrics` | A telemetry publisher must be composed per handle, not duplicated across shared pools. |
| `metrics_destination` | File paths/listeners need deployment ownership and collision/security handling. |
| `allow_remote_metrics` | Exposing a network listener is an explicit deployment security decision. |

The UI does not configure injected Python ports or per-operation arguments such
as query parameters, transaction mode, vector-space definitions or a bulk-load
policy. Those are API capabilities, not `DatabaseConfig` startup settings.

## Persistence and application

- `kg_grafx_buffer_pool_mb` is persisted alongside the existing ordinary controls.
- `kg_grafx_options` is a validated JSON object of advanced constructor overrides.
  The environment equivalent is `KG_GRAFX_OPTIONS='{"max_result_rows":1500}'`.
- An advanced-options PUT replaces that object as a unit; `{}` restores native
  defaults. An explicit `null` is accepted only for native optional limits.
- GET reports effective values separately from `desired_values`. Saving does not
  reconfigure live handles or restart Pulse automatically.
- After restart, the graph composition is recreated **after SQLite settings
  hydration and before seed/workers**. This prevents pools from retaining the
  app factory's pre-hydration defaults.
- Native `DatabaseConfig` validates values and combinations before persistence;
  ownership-controlled/unknown keys, booleans in integer budgets, non-finite values
  and numbers outside the exact JSON/UI range are refused.
- Options reach the writer, both reader pools, the shared Global pool and
  `open_unpooled` recovery/restore opens. Explicitly composed logical-transfer
  candidates retain their separate recovery-unit `connect_options` policy.
- A changed page size affects **new generations only**; no existing database is
  resized, reset or rebuilt by Settings. Other changes require process restart.
- Constructor option changes do not weaken multi-reader/writer, OCC, snapshots,
  checksums or acknowledged durability. Tight budgets can reject legitimate work;
  they are not silent result limits or a replacement for pagination.

No live settings were changed during validation. Persistence/restart and actual
native budget refusal were tested against isolated databases. The default buffer
budget remains 64 MiB; see `GRAFX_BUFFER_POOL_BUDGET.md` for the per-lane envelope.

Validation on 2026-09-07: 66 distinct Python tests across settings persistence,
native configuration, pools, composition and boot ordering; 19 UI tests; production
frontend build and packaged-asset sync passed. The restarted Pulse 0.3.3 returned
HTTP 200 for runtime settings (36 descriptors, 30 editable) and KG stats. Browser
interaction confirmed 36 help buttons, mouse/keyboard tooltips, preserved drafts
across tabs and Reset without saving. Native defaults and live settings were kept.

## Source milestone closeout

The isolated staged-source checkpoint passed 166 Community tests in 51.79 s
(settings, SQL persistence, actual native budgets, operational authorization and
edition provenance), plus 19 UI tests in 5.55 s. Ruff and staged whitespace checks
passed. The page-size REST field now requires an actual JSON integer; direct
service calls no longer truncate fractional sizes before native validation.
Persisted integer text remains supported.

The broader boundary test exposed two integration omissions: Core's manifest
did not declare the existing logical-transfer package, and the newly used
cancellation-safe blocking bridge was not an explicit public symbol. Core and
Community now agree on these narrow public contracts; private reach-in remains
zero. The final 39-test Core bridge/manifest/transfer-boundary slice passed.

Live Pulse PID 18920 still returns 36 descriptors, page size 8192, buffer 64 MiB,
descriptor mode generation, empty advanced overrides and no pending restart.
Browser verification confirmed all 36 help buttons and a tooltip under BODY,
outside the modal, with fixed positioning and z-index 100. Opening the tooltip
left the modal bounds unchanged (760 x 817.875 CSS px). Escape closed help while
leaving Settings open. Settings was then closed without saving.

The source milestone does not publish a wheel or replace the packaged frontend
bundle: the existing live assets already contain this UI, while their accumulated
source/release synchronization remains grouped with the pending DLQ UI milestone.
The new strict input guard and manifest registration need the next accumulated
runtime restart; they are not claimed hot-loaded in PID 18920. No live settings,
graph data, cognitive ledger, DLQ or benchmark specs were changed.
