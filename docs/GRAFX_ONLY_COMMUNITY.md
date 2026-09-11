# Grafx-only Community

Community uses `okto-grafx[accel]==0.0.5`. Ladybug/Kuzu is no longer a runtime,
installation dependency, configurable backend, recovery implementation or
logical-transfer endpoint. Core runtime and contracts remain unchanged and engine-agnostic: it
continues to consume graph ports and explicit capability declarations.

The shared executable architecture matrix and the paired Core README now describe
edition-owned graph adapters generically, rather than mandating a named database.

## Supported runtime

- Board and Global Discovery bindings select Grafx only. Independent Board read
  participants, transactions, generation authentication, WAL recovery, directory
  quarantine/restore and source revision fencing remain active.
- Per-board lifetime guards are in `graph_operation_guards.py`. Ordinary reads
  and writes can hold pins concurrently. Storage replacement drains those pins
  and fails closed on timeout. No process-wide Ladybug writer mutex remains.
- Global decision similarity search uses the read path, not writer authority.
- Reflective expansion enumerates typed endpoint paths up to three hops and
  executes them in one read batch/snapshot per edge family. It preserves
  multiple seeds, shortest-hop deduplication, endpoint layer filtering (without
  filtering intermediate nodes), and the 1,000-neighbor bound. No untyped
  native-engine path query or Core engine-specific condition is needed.
- Shutdown drains application probes and closes the actual composed Board read
  and write pools plus Global Discovery, retaining failure diagnostics.
- Settings publishes only Grafx constructor knobs, queue and decay controls.
  Removed knobs are not accepted by the Settings API. Existing persisted rows
  with those names are ignored, not silently translated into Grafx parameters.
- `kg_grafx_buffer_pool_mb` is a per-handle buffer budget, not an RSS limit or
  a graph size limit. The former Ladybug 2 GiB limit is not applied to Grafx.

## Existing data and compatibility

This change does **not** purge, backfill, rebuild or open a user's database.
SQLite tables/columns such as `kuzu_node_refs` retain their persisted names to
avoid unrelated data migrations. They contain application references, not an
embedded Kuzu dependency.

Bindings, generation manifests and existing Grafx directory paths retain their
identities. Some historical paths can include `.lbug`; the authenticated binding,
not a filename extension, identifies the engine. Renaming such paths implicitly
would invalidate durable authority and is deliberately not done.

A bound retired backend or an unbound legacy native file fails closed. Its bytes
are preserved; Community will not reinterpret it as Grafx, automatically replace
its binding, or run the removed Ladybug salvage/restore implementation. Retired
quarantine formats are rejected without changing files. Migration from such an
installation requires an explicit, separately governed data/rebuild procedure.

Privacy deletion remains explicit. The Grafx Board storage adapter removes both
its own generations and opaque historical `graph.lbug` artifacts/sidecars in the
validated board-owned namespace, under the same write fence. Alias/containment
checks happen before deletion, and the binding is removed last.

The old Ladybug-to-Grafx rollout/shadow-copy coordinator and paired-engine
M-PULSE-7 performance tools were retired. Existing rollout journal authority and
privacy tombstones are still honored; removing an engine must not silently
unlock a pending or ambiguous administrative operation.

## Verification

The removal has dedicated contracts in `tests/test_community_grafx_only.py` for
the dependency/import boundary, Settings schema, preservation/refusal of legacy
files, concurrent readers, fail-closed storage mutation and app composition with
native Ladybug/Kuzu imports actively blocked.

Retired native-driver tests are removed with their implementation; mixed test
modules retain engine-independent cases. Current replacements cover:

| Removed engine-specific coverage | Current Grafx coverage |
| --- | --- |
| Native per-board runtime and WAL | `test_grafx_board_operational_providers.py`, `test_grafx_board_recovery.py`, `test_grafx_directory_quarantine.py` |
| Native transaction, lineage and active-set writes | `test_grafx_graph_transaction.py`, `test_grafx_projection_active_set.py`, `test_grafx_spec_lineage.py`, `test_grafx_replace_payload_atomic_failure.py` |
| Native vector implementation/reference comparison | `test_grafx_board_vector_search.py`, `test_grafx_vector_acceptance_matrix.py` |
| Native Global recovery and writer chain | `test_grafx_global_discovery_providers.py`, `test_grafx_global_discovery_recovery_worker_extensions.py`, `test_grafx_global_recovery_batch.py`; neutral worker tests remain |
| Two-engine routing/intersection/shutdown | Updated `test_routed_*.py` and the Grafx-only release contracts |
| Native logical copying | `test_logical_transfer_grafx.py`, `test_grafx_logical_sink.py`, Grafx-only `test_logical_transfer_physical_matrix.py` |

### Validated on 2026-09-09

- Community collection: **4,868 tests**, no collection/import errors.
- The 1,335-case graph/routing/Settings/offline-recovery selection was split by
  a workstation reboot: the completed prefix was preserved and the remaining
  812 cases resumed. That run passed 805 and exposed seven stale contracts or
  integration failures; all seven were corrected and passed focused reruns.
  This is combined evidence, not a claim of one uninterrupted green run.
- Complementary adapter contracts: 184 passed together; the remaining migrated
  stale-sweep query test passed against real Grafx after using an explicit
  write transaction and the required fixture columns.
- CLI/bootstrap, architecture documentation, privacy ordering and relationship
  layout: **59 passed**. Final affected-surface rerun: **53 passed**, followed
  by a passing real three-hop expansion test after correcting its explicit
  endpoint hints. The added single-snapshot/deduplication contract also passed.
- Physical transfer/restore: **20 passed**, including concurrent source writes,
  phase failures, NULL/absent semantics and preserving the previous generation.
- Paired Core architecture matrix: **8 passed** (`--noconftest`); this does not
  claim a full Core regression or execution of its historical native fixtures.
- Frontend: **53 passed**, TypeScript/Vite build and packaged asset sync passed.
- Ruff `F821,E9` and `git diff --check`: passed.
- A clean installed environment contains neither `ladybug` nor `kuzu`, matches
  the built Community/Core wheels byte-for-byte and passes app composition,
  Settings, independent read/write visibility, recovery, cold verification,
  promotion, vector search, reopen and idempotency. Dependency check: 120
  compatible packages. All database smoke tests use disposable data homes.

The broad Community suite was not run to completion. Native-only tests and
paired-engine tools were retired; they are not reported as passing Grafx tests.
Logs and wheel artifacts for this local delivery are retained under the Grafx
checkout's `.grafx-tmp/ladybug-removal-tests/` directory.

### Local installation

Both launchers were updated from the verified local wheels: the Python 3.13
user installation and the uv-managed `okto-pulse` tool. Both now run Community
0.3.3 / Core 0.3.3 / `okto-grafx[accel]` 0.0.5. Ladybug 0.16.0 was uninstalled
from both environments; the unused Kuzu 0.11.3 distribution was also removed
from the Python user installation. No production database was removed or
opened for writes, and no Pulse server/backfill was started.

`python-global-smoke.log` and `uv-global-smoke.log` both confirm exact installed
Community/Core wheel bytes, absence of native drivers and retired adapters,
app/Settings composition, reader isolation, committed visibility, recovery,
cold verification, promotion, vector search, reopen and idempotency. The uv
environment's dependency check passes. No unrelated packages in the shared
Python installation were upgraded or repaired.

Installed wheel SHA-256:

- Community: `958d4513b6dc0bc60cbccb2c939daf3b4e226d65d56069edc594829976ebba81`
- Core: `6428f75752e8bbf7a58026617874ffa712aed7b8bd438608ee52012d9758dfca`

This is a local development build, not a new PyPI release. Reinstalling an
older published Pulse artifact can reintroduce its original dependencies;
use this checkout's wheels until the retirement is published.
