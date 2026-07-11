"""Community SQLAlchemy implementation of relational side-effect ports."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select

from okto_pulse.community.adapters.sqlalchemy_models import (
    Board,
    ConsolidationQueue,
    KGTickRun,
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
from okto_pulse.core.ports.kg_health import register_kg_health_read_port
from okto_pulse.core.ports.kg_governance import register_kg_governance_store
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
    register_resource_gate_service_class,
    register_runtime_settings_adapter,
    register_traceability_adapter,
)
from okto_pulse.core.ports.realm_access import register_realm_access_port


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

    async def upsert_consolidation_queue(
        self,
        session: Any,
        upsert: ConsolidationQueueUpsert,
    ) -> None:
        insert = _upsert_insert_for_session(session)
        stmt = (
            insert(ConsolidationQueue)
            .values(
                board_id=upsert.board_id,
                artifact_type=upsert.artifact_type,
                artifact_id=upsert.artifact_id,
                priority=upsert.priority,
                source=upsert.source,
                triggered_by_event=upsert.triggered_by_event,
                status="pending",
            )
            .on_conflict_do_update(
                index_elements=["board_id", "artifact_type", "artifact_id"],
                set_={
                    "status": "pending",
                    "attempts": 0,
                    "last_error": None,
                    "priority": upsert.priority,
                    "source": upsert.source,
                    "triggered_by_event": upsert.triggered_by_event,
                    "claimed_by_session_id": None,
                    "claimed_at": None,
                    "worker_id": None,
                    "claim_timeout_at": None,
                    "next_retry_at": None,
                },
                where=ConsolidationQueue.status.notin_(("pending", "claimed")),
            )
        )
        await session.execute(stmt)

    async def list_board_ids(self, session: Any) -> list[str]:
        result = await session.execute(select(Board.id))
        return list(result.scalars().all())

    async def read_latest_kg_tick_completed_at(
        self,
        session: Any,
    ) -> datetime | None:
        return (
            await session.execute(
                select(KGTickRun.completed_at)
                .where(KGTickRun.completed_at.is_not(None))
                .order_by(KGTickRun.completed_at.desc())
                .limit(1)
            )
        ).scalars().first()

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


def register_community_relational_effects() -> CommunitySqlAlchemyRelationalEffects:
    """Register Community relational side-effect implementation in core."""

    from okto_pulse.community.adapters.sqlalchemy_domain_event_delivery import (
        CommunitySqlAlchemyDomainEventFactReader,
        CommunitySqlAlchemyDomainEventPublisher,
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
    from okto_pulse.community.adapters.sqlalchemy_kg_health import (
        CommunitySqlAlchemyKGHealthReader,
    )
    from okto_pulse.community.adapters.sqlalchemy_kg_governance import (
        CommunitySqlAlchemyKGGovernanceStore,
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
    from okto_pulse.community.adapters.sqlalchemy_resource_gate_service import (
        ResourceGateService as CommunityResourceGateService,
    )
    from okto_pulse.community.adapters import (
        sqlalchemy_runtime_settings_service as runtime_settings_adapter,
        sqlalchemy_traceability_read_model as traceability_adapter,
    )

    register_relational_effects_port(_relational_effects)
    register_domain_event_publisher(CommunitySqlAlchemyDomainEventPublisher())
    register_domain_event_fact_reader(CommunitySqlAlchemyDomainEventFactReader())
    register_queue_health_read_port(CommunitySqlAlchemyQueueHealthReader())
    register_canonical_debt_store(CommunitySqlAlchemyCanonicalDebtStore())
    register_cognitive_effectiveness_read_port(
        CommunitySqlAlchemyCognitiveEffectivenessReader()
    )
    register_skip_override_read_port(CommunitySqlAlchemySkipOverrideReader())
    register_discovery_catalog_read_port(
        CommunitySqlAlchemyDiscoveryCatalogReader()
    )
    register_amendment_revision_store(
        CommunitySqlAlchemyAmendmentRevisionStore()
    )
    register_parent_artifact_read_port(CommunitySqlAlchemyParentArtifactReader())
    register_architecture_legacy_snapshot_read_port(
        CommunitySqlAlchemyArchitectureLegacySnapshotReader()
    )
    register_bug_regression_preview_read_port(
        CommunitySqlAlchemyBugRegressionPreviewReader()
    )
    register_discovery_selector_read_port(
        CommunitySqlAlchemyDiscoverySelectorReader()
    )
    register_board_relational_cleanup_port(
        CommunitySqlAlchemyBoardRelationalCleanup()
    )
    register_structured_spec_store(CommunitySqlAlchemyStructuredSpecStore())
    register_effective_resource_persistence_port(
        CommunitySqlAlchemyEffectiveResourcePersistence()
    )
    register_spec_resource_propagation_store(
        CommunitySqlAlchemySpecResourcePropagationStore()
    )
    register_critical_context_read_port(
        CommunitySqlAlchemyCriticalContextReader()
    )
    register_default_board_configuration_store(
        CommunitySqlAlchemyDefaultBoardConfigurationStore()
    )
    register_design_system_store(CommunitySqlAlchemyDesignSystemStore())
    register_global_outbox_store(CommunitySqlAlchemyGlobalOutboxStore())
    register_consolidation_persistence_port(
        CommunitySqlAlchemyConsolidationPersistence()
    )
    register_kg_health_read_port(CommunitySqlAlchemyKGHealthReader())
    register_kg_governance_store(CommunitySqlAlchemyKGGovernanceStore())
    register_discovery_execution_read_port(
        CommunitySqlAlchemyDiscoveryExecutionReader()
    )
    register_analytics_read_port(CommunitySqlAlchemyAnalyticsReader())
    register_architecture_persistence_port(
        CommunitySqlAlchemyArchitecturePersistence()
    )
    register_application_persistence_port(
        CommunitySqlAlchemyApplicationPersistence()
    )
    register_resource_gate_service_class(CommunityResourceGateService)
    register_runtime_settings_adapter(runtime_settings_adapter)
    register_traceability_adapter(traceability_adapter)
    register_realm_access_port(CommunitySqlAlchemyRealmAccess())
    return _relational_effects


__all__ = [
    "CommunitySqlAlchemyRelationalEffects",
    "register_community_relational_effects",
]
