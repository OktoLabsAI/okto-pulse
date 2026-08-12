# Architecture — Okto Pulse (Community edition)

Package layout, the core/edition ownership boundary, and every adapter Community supplies.


The `okto-pulse` package is the Community edition runtime for
`okto-pulse-core`. Core owns the domain, application services, REST/MCP
contracts and pure backend ports. Community owns the local runtime composition:
CLI, frontend bundle, local auth, storage, SQLite/LadybugDB wiring, telemetry
adapters and operational MCP resource overlays.

Community declares `fastmcp`, `uvicorn[standard]` and
`wsproto` directly. `CommunityMcpAuthenticator`,
`build_community_resource_catalog`, `CommunityCapabilityDescriptorSource`,
`build_mcp_trace_sink_from_env` and `JsonlMcpTraceSink` implement the local MCP
host concerns, including the public `okto_pulse.core.ports.McpTraceSink`
contract. Core supplies commands and contracts without owning the listener,
authentication backend, resource overlay or JSONL trace transport.

Core's architecture inventories and boundary gates are executable evidence that
concrete runtime adapters remain Community-owned. Community registers the local
adapters listed below and hosts the runtime components used by the single-node
distribution.

### AF-05/AF40 dependency owner matrix

The source of truth is Core's `dependency_ledger.py`,
`CANONICAL_AF40_DEPENDENCY_TOKENS`,
`CANONICAL_TEMPORARY_EXCEPTION_TOKENS` and `conformance_matrix.py`.
F14 dependency ownership keeps implementation dependencies out of the
published `okto-pulse-core` package and assigns local runtime dependencies to
Community.

| Dependency | Status | Community ownership |
| --- | --- | --- |
| `aiofiles` | `removed` | No runtime consumer; blocked from Core and Community manifests. |
| `requests` | `community_owned` | Telemetry HTTP adapter. |
| `chardet` | `community_owned` | Telemetry transport companion. |
| `aiosqlite` | `community_owned` | Local SQLite relational adapter. |
| `numpy` | `community_owned` | Local embedding and rerank stack. |
| `apscheduler` | `community_owned` | Local scheduler adapter. |

AF-11 did not move application logic from core into Community. The import-boundary
cleanup removed direct application-use-case imports of KG internals, transport
schemas, permission helpers and SQLAlchemy mutation helpers by adding
application-facing core facades. Community's role is unchanged: it packages the
updated core engine and supplies local-first adapters such as SQLite,
LadybugDB/Kuzu, filesystem storage, local ML providers, telemetry files and the
frontend bundle.

| AF-11 concern | Core responsibility | Community responsibility |
| --- | --- | --- |
| KG rules and orchestration | Application/KG rules exposed through `core.services.application_kg` and existing core KG contracts | Local graph/runtime adapters registered through the Community KG registry |
| REST/MCP payload contracts | DTO/schema compatibility exposed through `core.services.application_schemas` | Consume the packaged REST/MCP surface; no community-only MCP tools were added |
| Permission and transition policy | Authorization and gate policy exposed through `core.services.permission_policy` | Provide local auth/runtime context through Community adapters |
| Mutable persistence marking | Mutation intent exposed through `ApplicationPersistencePort`; no SQLAlchemy mutation helper remains in Core | Own SQLAlchemy mutable-column tracking, flush mechanics and local SQLite composition |
| Boundary evidence | `ImportBoundaryGate` and conformance tests prove the application-layer imports stay at zero | Boundary/conformance evidence adapters prove Community-owned adapter registration without core importing Community |

The AF-11 application-layer criterion remains
`ImportBoundaryGate(mode="bootstrap").observed_value == 0`. The final
decontamination gates additionally require the active relational, ORM,
singleton and private-reach-in budgets to remain zero; historical ledgers are
evidence only and cannot authorize new coupling.

AF-20 adds a stricter baseline policy that Community must respect when packaging
or registering local-first adapters. Core owns `IMPORT_BOUNDARY_BASELINE_LEDGER`,
the import-boundary gate, transition rules and singleton runtime ownership
checks; each accepted non-relational baseline needs owner, reason, removal criterion,
source spec/wave and risk. Community owns adapter-specific debt for
local concrete dependencies such as SQLite, LadybugDB/Kuzu, filesystem storage,
local ML providers and telemetry beacon sending. When a concrete dependency is
needed for the local runtime, wire it through a core port and register it in
Community instead of normalizing a new core baseline. The existing
`okto_pulse/core/ports` package is a real pure ports layer consumed by Community,
not a future target.

AF-21 and AF-28 define the Community Core Reach-In Ledger and public core
facades for the remaining direct imports from private core modules. The
executable gate is
`audit_community_core_import_boundary` in
`okto_pulse.community.adapters.core_import_boundary`. A Community adapter may
consume public core facades such as `okto_pulse.core.services.application_kg`,
`okto_pulse.core.services.application_agents`,
`okto_pulse.core.services.application_startup`, `okto_pulse.core.mcp`,
`okto_pulse.core.ports.*` and the adapter-neutral KG helpers in
`core.kg.board_source_store`, `core.kg.board_rebuild_adapter`,
`core.kg.tier_power`, `core.kg.scoring`, `core.kg.session_manager` and
`core.kg.global_discovery.schema`; direct reach-ins to ORM, database lifecycle,
KG workers/registry, `services.main`, private MCP server symbols, private core
helper symbols or core-owned concrete DDL constants must be removed or ledgered
with owner, reason, target public surface, removal path and withdrawal
criterion. Stale ledger entries fail the same gate, so deleting the last
reach-in for a dependency requires deleting its exception too.
Community database lifecycle and session composition must use
`okto_pulse.core.ports.relational_runtime`, not `core.infra.database`.

AF42 keeps that boundary executable instead of relying on prose. The current
release oracle is:

<!-- AF42-BOUNDARY-ORACLE:BEGIN -->
| Check | Current evidence |
| --- | --- |
| Historical private reach-in baseline | `32` |
| Current private reach-in budget | `0` |
| Current governed private reach-ins | `0` |
| Current full Community->Core import inventory | `1019` |
| Inventory classification | `public_contract=1019`, `governed_temporary_reach_in=0` |
| Boundary violations | `0` violations, `0` stale ledger entries, `0` incomplete ledger entries, `0` baseline-growth violations |
| Burn-down progression | `32 -> 21 -> 10 -> 0` after AF42 inventory, lifecycle/auth/MCP, then complete Community ORM ownership |
| Community release command | `python -m pytest tests/test_af21_core_import_boundary.py tests/test_af25_docs_truthfulness.py tests/test_af33_capstone_community_readiness.py tests/test_af35_s1_community_adapters.py tests/test_af35_s2_community_kg_operational_adapters.py tests/test_af41_runtime_dependency_ownership.py tests/test_af41_serving_boundary.py tests/test_r06_mcp_auth_context_community.py tests/test_r08a_mcp_auth_adapter.py tests/test_cli_init.py tests/test_cli_kg_backfill.py tests/test_hnd2_credential_surface_gate.py tests/test_r01c_imp4_schema_lifecycle_orchestrator.py tests/test_r16b_relational_schema_migrator.py tests/test_r16c_data_bootstrapper.py -q` -> `105 passed` |
| Core release command | `python -m pytest tests/test_boundary_audit_12.py tests/test_conformance_suite_15.py -q` -> `67 passed` |
<!-- AF42-BOUNDARY-ORACLE:END -->

Permitted Community->Core imports are narrow contracts: `core.ports.*`,
`core.application`, `core.domain`, `core.mcp`, the application facades under
`core.services.application_*`, and adapter-neutral KG helper surfaces already
listed above. Prohibited imports remain private implementation details:
`core.models.db`, `core.infra.database`, `core.services.main`,
`core.mcp.server`, `core.kg.workers.*`, `core.kg.governance`,
`core.kg.interfaces.registry` and concrete core-owned DDL constants. A
prohibited import may exist only as a governed temporary reach-in in
`COMMUNITY_CORE_REACH_IN_LEDGER`, with owner, reason, target public surface,
removal path and withdrawal criterion.

Bootstrap, schema migrations, CLI and seed paths must stay off
`core.models.db`; they use Community-owned row/SQL adapters or public facades.
The F13 provenance registry additionally proves that representative relational
and graph adapters are locally defined Community symbols implementing public
Core ports. Its bridge ledger and terminal bridge budget are both zero; adding a
private import, alias, reexport, dynamic import or constructor target is a
release-blocking failure.

### Adapters

Community registers its adapters from `okto_pulse.community.main` and
`okto_pulse.community.adapters.composition`. The main backend adapter package is
`src/okto_pulse/community/adapters`.

Registration flow:

- `create_community_app()` builds the local data directory, SQLite engine/session
  factory, auth/storage providers, runtime composition, REST app, MCP listener
  and frontend mount.
- `configure_community_kg_registry()` builds the Community KG registry and
  registers memory, embedding, rerank, telemetry, product, publish-health,
  graph, audit, event-bus and KG config providers before core consumers read the
  registry.
- `register_and_freeze_community_resource_catalog()` and
  `CommunityCapabilityDescriptorSource` extend the core MCP/resource metadata
  without importing Community from core.
- `CommunityRelationalSchemaMigrator` and `CommunityDataBootstrapper` are the
  initialization adapters consumed by `okto-pulse init` and startup.

Adapter source map:

- Runtime composition: `community/main.py` and
  `community/adapters/composition.py`; supporting provider wiring lives in
  `community/adapters/runtime_composition.py`.
- Scheduler and workers: `community/adapters/scheduler.py` and
  `community/adapters/workers.py`.
- Auth/storage/init: `community/auth.py`, `community/adapters/storage.py`,
  `community/adapters/relational_schema_migrator.py` and
  `community/adapters/data_bootstrapper.py`;
  `community/adapters/data_bootstrap_steps.py` owns the local bootstrap step
  implementations.
- Relational schema lifecycle: `community/adapters/relational_schema_lifecycle.py`
  and `community/adapters/relational_schema_steps.py`.
- Relational runtime: `community/adapters/sqlalchemy_database.py`,
  `community/adapters/sqlalchemy_unit_of_work.py`,
  `community/adapters/sqlalchemy_repositories.py`,
  `community/adapters/sqlalchemy_resource_gate_service.py`,
  `community/adapters/sqlalchemy_runtime_settings_service.py`,
  `community/adapters/sqlalchemy_traceability_read_model.py`,
  `community/adapters/coordination.py` and
  `community/adapters/relational_effects.py`; read-only sprint-lineage health is
  owned by `community/adapters/sprint_origin_integrity.py`; the SQLite PRAGMA owner is
  `install_community_sqlite_pragmas` in
  `community/adapters/sqlalchemy_database.py`.
- Relational mappings and persistence implementations:
  `community/adapters/sqlalchemy_*`.
- KG source/rebuild ingestion: `community/adapters/board_source_reader.py` and
  `community/adapters/board_rebuild_ingestion.py`; content ingestion helpers
  live in `community/adapters/content_ingestion.py`.
- Knowledge propagation rollout: `community/adapters/knowledge_propagation_backfill.py`.
- KG local schema/durability adapters: `community/adapters/global_discovery_*` and
  `community/adapters/rebuild_audit_storage.py`.
- Materialization-health adapters: `community/adapters/materialization_health.py`
  and `community/adapters/materialization_health_observability.py`.
- Terminal-debt recovery adapters: `community/adapters/terminal_debt_source.py`
  and `community/adapters/terminal_debt_snapshot.py`.
- KG outbox/audit persistence: `community/adapters/sqlite_outbox_event_bus.py`,
  `community/adapters/sqlalchemy_audit_repo.py` and
  `community/adapters/kg_operational.py`; generic KG queries exclude
  Code Traceability artifacts through
  `community/adapters/code_traceability_kg_sql.py` without inspecting source
  repositories.
- KG data and graph runtime: `community/adapters/data.py`,
  `community/adapters/memory.py`, `community/adapters/kg.py`,
  `community/adapters/kg_runtime.py`,
  `community/adapters/board_graph_runtime.py`,
  `community/adapters/global_discovery_runtime.py`,
  `community/adapters/ladybug_writer.py`,
  `community/adapters/graph_*`, `community/adapters/kg_*` and
  `community/adapters/kuzu_*`.
- ML search helpers: `community/adapters/embedding.py` and
  `community/adapters/rerank.py`; orchestration lives in
  `community/adapters/hybrid_search.py` and
  `community/adapters/reflective_query.py`.
- MCP/resource overlays and host runtime: `community/adapters/mcp_auth.py`,
  `community/adapters/mcp_admission.py`,
  `community/adapters/mcp_host.py`,
  `community/adapters/resources.py`,
  `community/adapters/capability_descriptors.py` and
  `community/adapters/mcp_trace.py` / `community/adapters/mcp_trace_middleware.py`.
- Trusted test-evidence execution and receipt verification:
  `community/adapters/test_evidence.py`.
- Canonical bug cognitive-context assembly:
  `community/adapters/bug_cognitive_context.py`.
- Relational application and KG event adapters:
  `community/adapters/relational_application.py` and
  `community/adapters/kg_events.py`; semantic-guideline KG events are emitted by
  `community/adapters/semantic_guideline_kg_events.py`.
- Externally-produced SK-A quality evidence persistence and bounded projection
  observability: `community/adapters/sqlalchemy_quality_assessment.py` and
  `community/adapters/ska_observability.py`; readers-first rollout capability
  checks live in
  `community/adapters/semantic_assessment_v2_capabilities.py`. Community does not run a
  Requirement Lint analyzer; an external agent submits evidence through the
  governed preflight/write contract.
- Telemetry: `community/adapters/telemetry_store.py`,
  `community/adapters/telemetry_sender.py`,
  `community/adapters/telemetry_state.py`,
  `community/adapters/telemetry_port.py`,
  `community/adapters/product_telemetry.py` and
  `community/adapters/publish_health_sources.py` and the grouped
  `community/adapters/telemetry_*` implementations.
- Boundary/conformance evidence: `community/adapters/readiness_evidence.py`,
  `community/adapters/data_dependency_audit.py`,
  `community/adapters/kg_dependency_audit.py`,
  `community/adapters/boundary_evidence.py`,
  `community/adapters/core_import_boundary.py`,
  `community/adapters/credential_surface_gate.py` and
  `community/adapters/smoke_evidence.py`.
- Telemetry effect defaults are supplied by
  `community/adapters/telemetry_effect_config.py`.
- Permission adapters: `community/adapters/permission_*`.
- Filesystem privacy erasure: `community/adapters/filesystem_erasure.py`.
- Ownership and local lifecycle support:
  `community/adapters/adapter_provenance.py`,
  `community/adapters/local_storage_ref.py`,
  `community/adapters/quarantine_restore.py`,
  `community/adapters/realm_migration.py`,
  `community/adapters/rebuild_effects.py`,
  `community/adapters/sqlite_only_boundary.py` and
  `community/adapters/worker_runners.py`.

Maintain this source map from the live filesystem under
`src/okto_pulse/community/adapters/**/*.py`, with `__init__.py` and private
helper modules prefixed with `_` excluded, and cross-check ownership against the
core `adapter_readiness_inventory` when a port is added or retired.

### Adapters built for the core ports

The source map above answers *"where does this file live?"*. This matrix answers the complementary
question: **which core port does each adapter implement?** Core declares the `Protocol`; Community
provides the only production implementation. **156 adapter modules** currently fill core's
~100 port protocols and 30 KG interfaces.

Anything core needs that is not in this table is either supplied by another edition or an unfilled
slot — and unfilled slots **fail closed** (`R-P2-03A-D`), never silently default.

**Persistence & relational**

| Core port | Community adapter |
|---|---|
| `ApplicationPersistencePort` | `CommunitySqlAlchemyApplicationPersistence` (+ `StatementBudget` guard) |
| `RelationalRuntime` | `CommunityDatabaseRuntime` — owns the SQLite PRAGMAs |
| `RelationalApplicationAdapter` · `PermissionPresetGateway` · `AgentAuthenticationGateway` | `CommunityRelationalApplicationAdapter` and siblings in `relational_application.py` |
| `RelationalEffectsPort` | `CommunitySqlAlchemyRelationalEffects` |
| `RelationalSchemaMigrator` · `RelationalSchemaLifecycleOrchestrator` | `CommunityRelationalSchemaMigrator` · `CommunityRelationalSchemaLifecycleOrchestrator` |
| `ResourceGateRelationalAdapter` | `CommunitySqlAlchemyResourceGateAdapter` |
| `CardRepositoryPort` and the board/ideation/spec repositories | `CommunityBoardRepository` · `CommunityIdeationRepository` · `CommunitySpecRepository` |
| `StructuredSpecStore` · `SpecMaterializationStore` · `SpecResourcePropagationStore` | `CommunitySqlAlchemyStructuredSpecStore` · `…SpecMaterializationStore` · `…SpecResourcePropagationStore` |
| `ArchitecturePersistencePort` · `ArchitectureLegacySnapshotReadPort` | `CommunitySqlAlchemyArchitecturePersistence` · `…ArchitectureLegacySnapshotReader` |
| `AmendmentRevisionStore` | `CommunitySqlAlchemyAmendmentRevisionStore` (+ `CommunityAmendmentRevisionApiBackend`) |
| `DefaultBoardConfigurationStore` · `DesignSystemStore` | `CommunitySqlAlchemyDefaultBoardConfigurationStore` · `…DesignSystemStore` |
| `EffectiveResourcePersistencePort` | `CommunitySqlAlchemyEffectiveResourcePersistence` |
| `BoardRelationalCleanupPort` | `CommunitySqlAlchemyBoardRelationalCleanup` |
| ORM mapping for all of the above | `sqlalchemy_models.py` (4 630 lines) · `CommunityUnitOfWork` / `CommunityUnitOfWorkFactory` |

**Knowledge Graph — storage & runtime**

| Core interface | Community adapter |
|---|---|
| `SemanticGraphStore` | `CommunityKuzuGraphStore` |
| `GraphTransactionScope` / `GraphTransaction` | `CommunityKuzuGraphTransaction` — owns the atomic tombstone swap and `TombstoneReplacementCompensationError` |
| `GraphLifecycle` · `GraphRuntimeStore` · `GraphSchemaManager` | `CommunityKuzuGraphLifecycle` · `CommunityKuzuGraphRuntimeStore` · `CommunityKuzuGraphSchemaManager` |
| `CypherExecutor` | `CommunityKuzuCypherExecutor` |
| `GlobalDiscoveryRuntime` | `CommunityGlobalDiscoveryRuntime` |
| `GlobalDiscoveryRecovery` | `CommunityGlobalDiscoveryRecovery` (+ preparation and worker modules) |
| `GraphRecovery` · `QuarantineRestore` | `CommunityGraphRecovery` (WAL salvage) · `CommunityQuarantineRestore` |
| board graph handle & pooling | `BoardConnection` / `BoardGraphHandle` in `kg_runtime.py` · `ConnectionPool` · `GraphMemoryPressure` · `LadybugWriterLease` |
| `AuditRepository` | `CommunityAuditRepository` |
| `SessionStore` · `CacheBackend` · `RateLimiter` | `CommunityInMemorySessionStore` · `CommunityInMemoryCache` · `CommunityInMemoryRateLimiter` |
| `RebuildAuditArtifactStore` (+ resolver) · `CognitivePendingWorkProvider` | `CommunityFileSystemRebuildAuditArtifactStore` · `…Resolver` · `CommunityFileSystemCognitivePendingWorkProvider` |
| `EmbeddingProvider` · `Reranker` | `CommunitySentenceTransformerProvider` / `CommunityStubEmbeddingProvider` · `CommunityCrossEncoderReranker` |
| `ReflectiveRetrievalPort` · `ReflectiveCriticPort` · `ReflectiveTelemetryPort` | `CommunityReflectiveRetrieval` · `CommunityDeterministicReflectiveCritic` · `CommunityReflectiveTelemetry` |
| `HopPlanner` / hybrid search | `KuzuVectorSeedProvider` · `KuzuGraphExpander` |
| `KGConfig` · `EventBus` | `CommunityKGConfig` · `CommunityOutboxEventBus` |
| composition of every KG slot | `CommunityKgComposition` |

**Knowledge Graph — governance & operations**

| Core port | Community adapter |
|---|---|
| `KGGovernanceStore` | `CommunitySqlAlchemyKGGovernanceStore` |
| `ConsolidationPersistencePort` | `CommunitySqlAlchemyConsolidationPersistence` |
| `KGOperationalReadModelPort` · `KGWorkerQueuePort` · `KGWorkerAuditPort` | `CommunitySqlAlchemyKGOperationalReadModel` · `…KGWorkerQueue` · `…KGWorkerAudit` (`CommunityKGOperationalPorts`) |
| `CognitiveSourceStore` (MKG-A) | `CommunitySqlAlchemyCognitiveSourceStore` |
| `EquivalenceLedger` (MKG-C) · `CurationProposalStore` | `CommunitySqlAlchemyEquivalenceLedger` · `…CurationProposalStore` |
| `NodeSubtypeRegistry` (MKG-E) | `CommunitySqlAlchemyNodeSubtypeRegistry` |
| `CanonicalDebtStore` | `CommunitySqlAlchemyCanonicalDebtStore` |
| `KGHealthReadPort` · `KGEventsReaderPort` | `CommunitySqlAlchemyKGHealthReader` · `CommunityKGEventsReader` |
| `MaterializationEvidencePort` | `CommunityMaterializationEvidenceProbe` · `CommunityMaterializationGenerationStore` · `CommunitySqlAlchemyMaterializationCensus` |
| `BugCognitiveContextAssembler` · `CanonicalBugNodeReadPort` | `CommunityBugCognitiveContextAssembler` · `CommunityCanonicalBugNodeReader` |
| `CognitiveEffectivenessReadPort` | `CommunitySqlAlchemyCognitiveEffectivenessReader` |
| rebuild ingestion & source reading | `CommunityBoardSourceReader` · `CommunityBoardRebuildIngestionAdapter` · `CommunityRebuildEffects` |

**Delivery, coordination & workers**

| Core port | Community adapter |
|---|---|
| `LeaseProvider` · `WriteLockPort` · `ClaimRepository` · `RuntimeSettingsProvider` | `CommunityLocalLeaseProvider` · `CommunityLocalWriteLockPort` · `CommunitySqlAlchemyClaimRepository` · `CommunityRuntimeSettingsProvider` |
| `GlobalOutboxStore` · `DeliveryLedgerPort` | `CommunitySqlAlchemyGlobalOutboxStore` · `CommunitySqlAlchemyDeliveryLedger` |
| `DomainEventDeliveryStore` · `DomainEventPublisher` · `DomainEventFactReader` | `CommunitySqlAlchemyDomainEventDeliveryStore` · `…DomainEventPublisher` · `…DomainEventFactReader` |
| `WorkerClockPort` · `BlockingExecutionPort` · `QueueWorkPort` · `OutboxWorkPort` | `UtcWorkerClock` · `TrackedBlockingExecution` · `PollingRunner` · `ConsolidationRunner` |
| `SchedulerControl` | `SingletonSchedulerControl` |
| runtime settings snapshot | `CommunitySettingsSnapshotProvider` |

**MCP, identity & inbound**

| Core port | Community adapter |
|---|---|
| `McpHostProvider` | `CommunityMcpHostProvider` (+ outcome-validation, API-key and composition middlewares) |
| `McpAuthenticator` | `CommunityMcpAuthenticator` / `MCPAuthContext` |
| `McpResourceCatalog` · `McpInstructionProvider` | `register_and_freeze_community_resource_catalog()` · `CommunityFileMcpInstructionProvider` |
| `McpTraceSink` | `JsonlMcpTraceSink` (+ `CommunityTraceMiddleware`) |
| `CapabilityDescriptorSource` | `CommunityCapabilityDescriptorSource` |
| `PermissionPolicyPort` · `PermissionPresetReconciliationRepository` | `CommunityPermissionPolicyAdapter` · `CommunityPermissionPresetReconciliationRepository` |
| `RealmAccessPort` | `CommunitySqlAlchemyRealmAccess` |

**Read models & reporting**

| Core port | Community adapter |
|---|---|
| `AnalyticsReadPort` | `CommunitySqlAlchemyAnalyticsReader` |
| `TraceabilityReadPort` | `sqlalchemy_traceability_read_model.py` |
| `DiscoveryCatalogReadPort` · `DiscoveryExecutionReadPort` · `DiscoverySelectorReadPort` | `CommunitySqlAlchemyDiscoveryCatalogReader` · `…DiscoveryExecutionReader` · `…DiscoverySelectorReader` |
| `QueueHealthReadPort` · `TakedownTelemetryReadPort` | `CommunitySqlAlchemyQueueHealthReader` · `CommunitySqlAlchemyTakedownTelemetry` |
| `CriticalContextReadPort` | `CommunitySqlAlchemyCriticalContextReader` |
| `BugRegressionPreviewReadPort` | `CommunitySqlAlchemyBugRegressionPreviewReader` |
| `ParentArtifactReadPort` · `SkipOverrideReadPort` | `CommunitySqlAlchemyParentArtifactReader` · `CommunitySqlAlchemySkipOverrideReader` |

**Content, storage, telemetry & bootstrap**

| Core port | Community adapter |
|---|---|
| storage provider | `CommunityFileSystemStorage` |
| `ContentIngestionResolver` | `CommunityContentIngestionResolver` (SSRF-guarded remote target validation) |
| `TestEvidenceWriteVerifier` · `TestEvidenceExecutionIssuer` | `CommunityHttpManifestExecutor` and siblings in `test_evidence.py` |
| `KnowledgePropagationPort` · `KnowledgeMutationAuditSink` | `CommunitySqlAlchemyKnowledgePropagationStore` (+ backfill rollout) |
| `TelemetryStateStore` · `TelemetryStateCarrier` · `TelemetryPort` · `TelemetryEventStore` | `CommunityLocalTelemetryStore` · `CommunityTelemetryStateCarrier` · `CommunityTelemetryService` · `CommunityTelemetryBeaconSender` |
| `PublishHealthSource` · `ProductAggregationPort` · `TelemetryEffectConfigProvider` | `LocalPublishHealthSource` / `InstallLifecycleSource` / `AwsIngestSource` / `ReportAthenaSource` · `CommunityProductTelemetryAggregator` · `CommunityTelemetryEffectConfigProvider` |
| `DataBootstrapper` | `CommunityDataBootstrapper` |

**Boundary evidence (not core ports — they police the boundary itself)**

`core_import_boundary.py` ledgers every Community→core reach-in · `sqlite_only_boundary.py` ledgers
raw-SQLite residuals · `credential_surface_gate.py` scans for credential exposure ·
`boundary_evidence.py` carries the conformance result · `kg_chaos_executor.py` drives fault injection
against the graph adapters.

---

[← Back to README](../README.md)
