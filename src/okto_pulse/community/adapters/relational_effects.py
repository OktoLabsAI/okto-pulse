"""Community SQLAlchemy implementation of relational side-effect ports."""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import exists, func, literal, select, update

from okto_pulse.community.adapters.sqlalchemy_models import (
    ArtifactDeletionTombstone,
    Board,
    ConsolidationQueue,
    GlobalDiscoveryRecoveryAttempt,
    GlobalDiscoveryRecoverySlot,
    KGTickRun,
)
from okto_pulse.core.ports.global_discovery_recovery_control import (
    GLOBAL_RECOVERY_SLOT_ID,
)
from okto_pulse.core.ports.relational_effects import (
    ConsolidationQueueUpsert,
    KGTickRunUpsert,
    RelationalEffectsPort,
    register_relational_effects_port,
)
from okto_pulse.core.ports.domain_event_delivery import (
    register_domain_event_fact_reader,
    register_domain_event_publisher,
)
from okto_pulse.core.ports.bug_cognitive_context import (
    register_bug_cognitive_context_assembler,
    register_canonical_bug_node_read_port,
)
from okto_pulse.core.ports.test_evidence import (
    register_test_evidence_execution_issuer,
    register_test_evidence_write_verifier,
)
from okto_pulse.core.ports.queue_health import register_queue_health_read_port
from okto_pulse.core.ports.canonical_debt import register_canonical_debt_store
from okto_pulse.core.ports.cognitive_effectiveness import (
    register_cognitive_effectiveness_read_port,
)
from okto_pulse.core.ports.skip_overrides import register_skip_override_read_port
from okto_pulse.core.ports.discovery_catalog import (
    register_discovery_catalog_read_port,
)
from okto_pulse.core.ports.amendment_revision import (
    register_amendment_revision_store,
)
from okto_pulse.core.ports.parent_artifact import register_parent_artifact_read_port
from okto_pulse.core.ports.architecture_legacy import (
    register_architecture_legacy_snapshot_read_port,
)
from okto_pulse.core.ports.bug_regression_preview import (
    register_bug_regression_preview_read_port,
)
from okto_pulse.core.ports.discovery_selector import (
    register_discovery_selector_read_port,
)
from okto_pulse.core.ports.board_relational_cleanup import (
    register_board_relational_cleanup_port,
)
from okto_pulse.core.ports.structured_spec import register_structured_spec_store
from okto_pulse.core.ports.effective_resource import (
    register_effective_resource_persistence_port,
)
from okto_pulse.core.ports.spec_resource_propagation import (
    register_spec_resource_propagation_store,
)
from okto_pulse.core.ports.critical_context import register_critical_context_read_port
from okto_pulse.core.ports.default_board_configuration import (
    register_default_board_configuration_store,
)
from okto_pulse.core.ports.design_system import register_design_system_store
from okto_pulse.core.ports.global_outbox import register_global_outbox_store
from okto_pulse.core.ports.consolidation import (
    register_consolidation_persistence_port,
)
from okto_pulse.core.ports.delivery_ledger import register_delivery_ledger_port
from okto_pulse.core.ports.reconcile_intent import register_reconcile_intent_port
from okto_pulse.core.ports.stale_sweep import register_stale_sweep_port
from okto_pulse.core.ports.takedown_telemetry import (
    register_takedown_telemetry_read_port,
)
from okto_pulse.core.ports.tombstone import register_tombstone_port
from okto_pulse.core.ports.kg_health import register_kg_health_read_port
from okto_pulse.core.ports.kg_governance import register_kg_governance_store
from okto_pulse.core.ports.knowledge_propagation import (
    register_knowledge_mutation_audit_sink,
    register_knowledge_propagation_port,
)
from okto_pulse.core.ports.discovery_execution import (
    register_discovery_execution_read_port,
)
from okto_pulse.core.ports.analytics_read import register_analytics_read_port
from okto_pulse.core.ports.architecture_persistence import (
    register_architecture_persistence_port,
)
from okto_pulse.core.ports.application_persistence import (
    register_application_persistence_port,
)
from okto_pulse.core.ports.relational_services import (
    register_resource_gate_adapter_factory,
    register_runtime_settings_adapter,
    register_traceability_adapter,
)
from okto_pulse.core.ports.realm_access import register_realm_access_port
from okto_pulse.core.ports.policy_constraint_projection import (
    register_policy_constraint_projection_port,
)
from okto_pulse.core.ports.code_traceability_event_effects import (
    register_code_traceability_event_effects_port,
)


class CommunitySqlAlchemyRelationalEffects(RelationalEffectsPort):
    """Community-owned SQLAlchemy implementation for core runtime effects."""

    async def count_active_consolidation_queue(
        self,
        session: Any,
        *,
        board_id: str,
    ) -> int:
        depth = await session.scalar(
            select(func.count()).where(
                ConsolidationQueue.board_id == board_id,
                ConsolidationQueue.status.in_(["pending", "claimed"]),
            )
        )
        return int(depth or 0)

    async def upsert_consolidation_queue_unless_tombstoned(
        self,
        session: Any,
        upsert: ConsolidationQueueUpsert,
    ) -> bool:
        insert = _upsert_insert_for_session(session)
        candidate_id = str(uuid.uuid4())
        admitted_values = select(
            literal(candidate_id),
            literal(upsert.board_id),
            literal(upsert.artifact_type),
            literal(upsert.artifact_id),
            literal("consolidate"),
            literal(0),
            literal(upsert.priority),
            literal(upsert.source),
            literal(upsert.triggered_by_event),
            literal("pending"),
            literal(0),
        ).where(
            ~exists(
                select(1).where(
                    ArtifactDeletionTombstone.board_id == upsert.board_id,
                    ArtifactDeletionTombstone.artifact_type == upsert.artifact_type,
                    ArtifactDeletionTombstone.artifact_id == upsert.artifact_id,
                )
            )
        )
        stmt = (
            insert(ConsolidationQueue)
            .from_select(
                (
                    "id",
                    "board_id",
                    "artifact_type",
                    "artifact_id",
                    "work_kind",
                    "generation",
                    "priority",
                    "source",
                    "triggered_by_event",
                    "status",
                    "attempts",
                ),
                admitted_values,
                include_defaults=False,
            )
            .on_conflict_do_update(
                index_elements=["board_id", "artifact_type", "artifact_id"],
                index_where=ConsolidationQueue.work_kind == "consolidate",
                set_={
                    "status": "pending",
                    "attempts": 0,
                    "last_error": None,
                    "priority": upsert.priority,
                    "source": upsert.source,
                    "triggered_by_event": upsert.triggered_by_event,
                    "claimed_by_session_id": None,
                    "claim_token": None,
                    "claimed_at": None,
                    "worker_id": None,
                    "claim_timeout_at": None,
                    "next_retry_at": None,
                },
                # A duplicate while still pending is already represented by
                # the durable row. A new semantic event racing an active
                # worker is different: invalidate that exact claim and leave
                # the row pending so the stale graph snapshot cannot ACK.
                # The Core worker's final claim fence and compare-and-delete
                # ACK then either abort before graph commit or compensate its
                # deferred graph mutation after losing the ACK CAS.
                where=ConsolidationQueue.status != "pending",
            )
            .returning(ConsolidationQueue.id)
        )
        return (await session.execute(stmt)).scalar_one_or_none() is not None

    async def list_board_ids(self, session: Any) -> list[str]:
        result = await session.execute(select(Board.id))
        return list(result.scalars().all())

    async def is_global_recovery_active(self, session: Any) -> bool:
        active = await session.scalar(
            select(
                exists(
                    select(1).where(
                        GlobalDiscoveryRecoverySlot.slot_id == GLOBAL_RECOVERY_SLOT_ID
                    )
                )
                | exists(
                    select(1).where(
                        GlobalDiscoveryRecoveryAttempt.state.in_(("pending", "running"))
                    )
                )
            )
        )
        return bool(active)

    async def fence_kg_tick_publication(self, session: Any) -> bool:
        """Acquire SQLite's writer slot for the caller's short publication UoW.

        Even when no recovery slot row exists, executing this no-op UPDATE
        starts SQLite's write transaction. Recovery admission uses
        ``BEGIN IMMEDIATE`` against the same database, so exactly one side can
        win the check-and-publish/check-and-admit window. The caller publishes
        its durable event before releasing the transaction.
        """

        await session.execute(
            update(GlobalDiscoveryRecoverySlot)
            .where(GlobalDiscoveryRecoverySlot.slot_id == GLOBAL_RECOVERY_SLOT_ID)
            .values(version=GlobalDiscoveryRecoverySlot.version)
        )
        return await self.is_global_recovery_active(session)

    async def read_latest_kg_tick_completed_at(
        self,
        session: Any,
    ) -> datetime | None:
        return (
            (
                await session.execute(
                    select(KGTickRun.completed_at)
                    .where(KGTickRun.completed_at.is_not(None))
                    .order_by(KGTickRun.completed_at.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )

    async def upsert_kg_tick_run(
        self,
        session: Any,
        upsert: KGTickRunUpsert,
    ) -> None:
        insert = _upsert_insert_for_session(session)
        stmt = (
            insert(KGTickRun)
            .values(
                tick_id=upsert.tick_id,
                started_at=upsert.started_at,
                completed_at=upsert.completed_at,
                nodes_recomputed=upsert.nodes_recomputed,
                duration_ms=upsert.duration_ms,
                boards_processed=upsert.boards_processed,
                boards_failed=upsert.boards_failed,
                error=upsert.error,
            )
            .on_conflict_do_update(
                index_elements=["tick_id"],
                set_={
                    "completed_at": upsert.completed_at,
                    "nodes_recomputed": upsert.nodes_recomputed,
                    "duration_ms": upsert.duration_ms,
                    "boards_processed": upsert.boards_processed,
                    "boards_failed": upsert.boards_failed,
                    "error": upsert.error,
                },
            )
        )
        await session.execute(stmt)


def _upsert_insert_for_session(session: Any):
    del session
    from sqlalchemy.dialects.sqlite import insert

    return insert


_relational_effects = CommunitySqlAlchemyRelationalEffects()


def register_community_relational_effects(
    *,
    settings: Any | None = None,
    api_base_url: str | None = None,
) -> CommunitySqlAlchemyRelationalEffects:
    """Register Community relational side-effect implementation in core."""

    from okto_pulse.community.adapters.sqlalchemy_domain_event_delivery import (
        CommunitySqlAlchemyDomainEventFactReader,
        CommunitySqlAlchemyDomainEventPublisher,
    )
    from okto_pulse.community.adapters.bug_cognitive_context import (
        CommunityBugCognitiveContextAssembler,
        CommunityCanonicalBugNodeReader,
    )
    from okto_pulse.community.adapters.test_evidence import (
        CommunityEvidenceLedger,
        CommunityHttpManifestExecutor,
        CommunityTestEvidenceExecutionIssuer,
        CommunityTestEvidenceWriteVerifier,
    )
    from okto_pulse.community.adapters.sqlalchemy_queue_health import (
        CommunitySqlAlchemyQueueHealthReader,
    )
    from okto_pulse.community.adapters.sqlalchemy_canonical_debt import (
        CommunitySqlAlchemyCanonicalDebtStore,
    )
    from okto_pulse.community.adapters.sqlalchemy_cognitive_effectiveness import (
        CommunitySqlAlchemyCognitiveEffectivenessReader,
    )
    from okto_pulse.community.adapters.sqlalchemy_skip_overrides import (
        CommunitySqlAlchemySkipOverrideReader,
    )
    from okto_pulse.community.adapters.sqlalchemy_discovery_catalog import (
        CommunitySqlAlchemyDiscoveryCatalogReader,
    )
    from okto_pulse.community.adapters.sqlalchemy_amendment_revision import (
        CommunitySqlAlchemyAmendmentRevisionStore,
    )
    from okto_pulse.community.adapters.sqlalchemy_parent_artifact import (
        CommunitySqlAlchemyParentArtifactReader,
    )
    from okto_pulse.community.adapters.sqlalchemy_architecture_legacy import (
        CommunitySqlAlchemyArchitectureLegacySnapshotReader,
    )
    from okto_pulse.community.adapters.sqlalchemy_bug_regression_preview import (
        CommunitySqlAlchemyBugRegressionPreviewReader,
    )
    from okto_pulse.community.adapters.sqlalchemy_discovery_selector import (
        CommunitySqlAlchemyDiscoverySelectorReader,
    )
    from okto_pulse.community.adapters.sqlalchemy_board_cleanup import (
        CommunitySqlAlchemyBoardRelationalCleanup,
    )
    from okto_pulse.community.adapters.sqlalchemy_structured_spec import (
        CommunitySqlAlchemyStructuredSpecStore,
    )
    from okto_pulse.community.adapters.sqlalchemy_effective_resource import (
        CommunitySqlAlchemyEffectiveResourcePersistence,
    )
    from okto_pulse.community.adapters.sqlalchemy_spec_resource_propagation import (
        CommunitySqlAlchemySpecResourcePropagationStore,
    )
    from okto_pulse.community.adapters.sqlalchemy_critical_context import (
        CommunitySqlAlchemyCriticalContextReader,
    )
    from okto_pulse.community.adapters.sqlalchemy_default_board_configuration import (
        CommunitySqlAlchemyDefaultBoardConfigurationStore,
    )
    from okto_pulse.community.adapters.sqlalchemy_design_system import (
        CommunitySqlAlchemyDesignSystemStore,
    )
    from okto_pulse.community.adapters.sqlalchemy_global_outbox import (
        CommunitySqlAlchemyGlobalOutboxStore,
    )
    from okto_pulse.community.adapters.sqlalchemy_consolidation import (
        CommunitySqlAlchemyConsolidationPersistence,
    )
    from okto_pulse.community.adapters.sqlalchemy_delivery_ledger import (
        CommunitySqlAlchemyDeliveryLedger,
    )
    from okto_pulse.community.adapters.sqlalchemy_takedown_telemetry import (
        CommunitySqlAlchemyTakedownTelemetry,
    )
    from okto_pulse.community.adapters.sqlalchemy_kg_health import (
        CommunitySqlAlchemyKGHealthReader,
    )
    from okto_pulse.community.adapters.sqlalchemy_kg_governance import (
        CommunitySqlAlchemyKGGovernanceStore,
    )
    from okto_pulse.community.adapters.sqlalchemy_knowledge_propagation import (
        CommunitySqlAlchemyKnowledgePropagationStore,
    )
    from okto_pulse.community.adapters.sqlalchemy_discovery_execution import (
        CommunitySqlAlchemyDiscoveryExecutionReader,
    )
    from okto_pulse.community.adapters.sqlalchemy_analytics_read import (
        CommunitySqlAlchemyAnalyticsReader,
    )
    from okto_pulse.community.adapters.sqlalchemy_architecture_persistence import (
        CommunitySqlAlchemyArchitecturePersistence,
    )
    from okto_pulse.community.adapters.sqlalchemy_application_persistence import (
        CommunitySqlAlchemyApplicationPersistence,
    )
    from okto_pulse.community.adapters.sqlalchemy_realm_access import (
        CommunitySqlAlchemyRealmAccess,
    )
    from okto_pulse.community.adapters.sqlalchemy_policy_constraint_projection import (
        CommunitySqlAlchemyPolicyConstraintProjection,
    )
    from okto_pulse.community.adapters.sqlalchemy_code_traceability_event_effects import (
        CommunitySqlAlchemyCodeTraceabilityEventEffects,
    )
    from okto_pulse.community.adapters.sqlalchemy_resource_gate_service import (
        CommunitySqlAlchemyResourceGateAdapter,
    )
    from okto_pulse.community.adapters import (
        sqlalchemy_runtime_settings_service as runtime_settings_adapter,
        sqlalchemy_traceability_read_model as traceability_adapter,
    )

    register_relational_effects_port(_relational_effects)
    register_domain_event_publisher(CommunitySqlAlchemyDomainEventPublisher())
    register_domain_event_fact_reader(CommunitySqlAlchemyDomainEventFactReader())
    canonical_bug_reader = CommunityCanonicalBugNodeReader()
    register_canonical_bug_node_read_port(canonical_bug_reader)
    register_bug_cognitive_context_assembler(
        CommunityBugCognitiveContextAssembler(canonical_bug_reader)
    )
    if settings is None:
        from okto_pulse.community.config import CommunitySettings

        settings = CommunitySettings()
    evidence_ledger = CommunityEvidenceLedger(
        evidence_root=Path(settings.data_dir) / "evidence"
    )
    runtime_executor = CommunityHttpManifestExecutor(
        base_url=api_base_url or f"http://127.0.0.1:{settings.port}"
    )
    register_test_evidence_write_verifier(
        CommunityTestEvidenceWriteVerifier(ledger=evidence_ledger)
    )
    register_test_evidence_execution_issuer(
        CommunityTestEvidenceExecutionIssuer(
            ledger=evidence_ledger,
            executor=runtime_executor,
            environment=str(getattr(settings, "environment", "local")),
        )
    )
    register_queue_health_read_port(CommunitySqlAlchemyQueueHealthReader())
    register_canonical_debt_store(CommunitySqlAlchemyCanonicalDebtStore())
    register_cognitive_effectiveness_read_port(
        CommunitySqlAlchemyCognitiveEffectivenessReader()
    )
    register_skip_override_read_port(CommunitySqlAlchemySkipOverrideReader())
    register_discovery_catalog_read_port(CommunitySqlAlchemyDiscoveryCatalogReader())
    register_amendment_revision_store(CommunitySqlAlchemyAmendmentRevisionStore())
    register_parent_artifact_read_port(CommunitySqlAlchemyParentArtifactReader())
    register_architecture_legacy_snapshot_read_port(
        CommunitySqlAlchemyArchitectureLegacySnapshotReader()
    )
    register_bug_regression_preview_read_port(
        CommunitySqlAlchemyBugRegressionPreviewReader()
    )
    register_discovery_selector_read_port(CommunitySqlAlchemyDiscoverySelectorReader())
    register_board_relational_cleanup_port(CommunitySqlAlchemyBoardRelationalCleanup())
    register_structured_spec_store(CommunitySqlAlchemyStructuredSpecStore())
    register_effective_resource_persistence_port(
        CommunitySqlAlchemyEffectiveResourcePersistence()
    )
    register_spec_resource_propagation_store(
        CommunitySqlAlchemySpecResourcePropagationStore()
    )
    register_critical_context_read_port(CommunitySqlAlchemyCriticalContextReader())
    register_default_board_configuration_store(
        CommunitySqlAlchemyDefaultBoardConfigurationStore()
    )
    register_design_system_store(CommunitySqlAlchemyDesignSystemStore())
    register_global_outbox_store(CommunitySqlAlchemyGlobalOutboxStore())
    governed_deletion_persistence = CommunitySqlAlchemyConsolidationPersistence()
    register_consolidation_persistence_port(governed_deletion_persistence)
    register_tombstone_port(governed_deletion_persistence)
    register_reconcile_intent_port(governed_deletion_persistence)
    register_stale_sweep_port(governed_deletion_persistence)
    delivery_ledger = CommunitySqlAlchemyDeliveryLedger()
    register_delivery_ledger_port(delivery_ledger)
    register_takedown_telemetry_read_port(
        CommunitySqlAlchemyTakedownTelemetry(delivery_ledger)
    )
    register_kg_health_read_port(CommunitySqlAlchemyKGHealthReader())
    register_kg_governance_store(CommunitySqlAlchemyKGGovernanceStore())
    from okto_pulse.community.adapters.sqlalchemy_database import get_session_factory

    knowledge_propagation_store = CommunitySqlAlchemyKnowledgePropagationStore(
        lambda: get_session_factory()()
    )
    register_knowledge_propagation_port(knowledge_propagation_store)
    register_knowledge_mutation_audit_sink(knowledge_propagation_store)
    register_discovery_execution_read_port(
        CommunitySqlAlchemyDiscoveryExecutionReader()
    )
    register_analytics_read_port(CommunitySqlAlchemyAnalyticsReader())
    register_architecture_persistence_port(CommunitySqlAlchemyArchitecturePersistence())
    register_application_persistence_port(CommunitySqlAlchemyApplicationPersistence())
    register_resource_gate_adapter_factory(CommunitySqlAlchemyResourceGateAdapter)
    register_runtime_settings_adapter(runtime_settings_adapter)
    register_traceability_adapter(traceability_adapter)
    register_realm_access_port(CommunitySqlAlchemyRealmAccess())
    register_policy_constraint_projection_port(
        CommunitySqlAlchemyPolicyConstraintProjection()
    )
    register_code_traceability_event_effects_port(
        CommunitySqlAlchemyCodeTraceabilityEventEffects()
    )
    return _relational_effects


__all__ = [
    "CommunitySqlAlchemyRelationalEffects",
    "register_community_relational_effects",
]
