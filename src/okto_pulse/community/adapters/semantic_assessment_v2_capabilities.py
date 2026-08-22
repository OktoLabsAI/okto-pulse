"""Runtime readers-first capability probe for semantic assessment v2."""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.core import get_settings
from okto_pulse.core.ports.semantic_subject_projection import (
    SemanticAssessmentV2CapabilitySnapshot,
)

from .relational_schema_steps import (
    semantic_pinpoint_v2_postgresql_ddl,
    semantic_pinpoint_v2_sqlite_trigger_manifest,
)


_V2_TABLES = frozenset(
    {
        "semantic_guideline_assessments_v2",
        "semantic_guideline_metric_results_v2",
        "semantic_guideline_findings_v2",
    }
)


class CommunitySemanticAssessmentV2Capabilities:
    """Prove deployed prerequisites before enabling the single v2 writer."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        rest_transport_ready: bool = True,
        mcp_transport_ready: bool = True,
    ) -> None:
        self._session = session
        self._rest_transport_ready = rest_transport_ready
        self._mcp_transport_ready = mcp_transport_ready

    async def semantic_assessment_v2_capabilities(
        self,
    ) -> SemanticAssessmentV2CapabilitySnapshot:
        settings = get_settings()
        connection = await self._session.connection()
        tables = set(
            await connection.run_sync(
                lambda sync_connection: inspect(sync_connection).get_table_names()
            )
        )
        storage_ready = _V2_TABLES <= tables
        dialect = connection.dialect.name
        if dialect == "sqlite":
            rows = (
                await connection.execute(
                    text("SELECT name FROM sqlite_master WHERE type='trigger'")
                )
            ).all()
            actual = {str(row[0]) for row in rows}
            expected = set(semantic_pinpoint_v2_sqlite_trigger_manifest())
            triggers_ready = expected <= actual
        elif dialect == "postgresql":
            rows = (
                await connection.execute(
                    text(
                        "SELECT tg.tgname FROM pg_trigger AS tg "
                        "WHERE NOT tg.tgisinternal"
                    )
                )
            ).all()
            actual = {str(row[0]) for row in rows}
            _function_sql, specifications = semantic_pinpoint_v2_postgresql_ddl()
            triggers_ready = set(specifications) <= actual
        else:
            triggers_ready = False
        return SemanticAssessmentV2CapabilitySnapshot(
            readers_ready=bool(settings.semantic_assessment_v2_readers_ready),
            storage_ready=storage_ready,
            triggers_ready=triggers_ready,
            rest_transport_ready=self._rest_transport_ready,
            mcp_transport_ready=self._mcp_transport_ready,
            writer_requested=bool(settings.semantic_assessment_v2_writer_enabled),
        )


__all__ = ["CommunitySemanticAssessmentV2Capabilities"]
