# Grafx in Menu > Settings

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
