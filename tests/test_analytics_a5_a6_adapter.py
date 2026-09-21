from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.relational_application import (
    CommunityRelationalApplicationAdapter,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Base,
    Board,
)
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import (
    CommunitySemanticSession,
)
from okto_pulse.community.api.analytics_transport import (
    CanonicalBoardKgAnalyticsResponseDTO,
)
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.core.application.use_cases.board_kg_analytics import (
    BoardKgAnalyticsCommand,
    BoardKgAnalyticsUseCase,
)
from okto_pulse.core.ports.analytics_foundation import AnalyticsUtcWindow
from okto_pulse.core.ports.relational_application import (
    RelationalApplicationAdapter,
    register_relational_application_adapter,
)


NOW = datetime(2026, 8, 21, 12, tzinfo=UTC)
BOARD_ID = "11111111-1111-4111-8111-111111111111"
SPEC_ID = "22222222-2222-4222-8222-222222222222"


@pytest.mark.asyncio
async def test_board_kg_is_reachable_through_real_uow_without_sprint_forecast() -> None:
    engine = create_async_engine("sqlite+aiosqlite://", future=True)
    factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        sync_session_class=CommunitySemanticSession,
        expire_on_commit=False,
    )
    adapter = CommunityRelationalApplicationAdapter()
    assert isinstance(adapter, RelationalApplicationAdapter)
    register_relational_application_adapter(adapter)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            session.add(
                Board(
                    id=BOARD_ID,
                    name="Analytics E2E",
                    owner_id="user-1",
                    realm_id="local",
                )
            )
            await session.commit()

        actor = RESTAdapterContract.actor("user-1", board_id=BOARD_ID)
        async with factory() as session:
            uow = CommunityUnitOfWork(session)
            kg = await BoardKgAnalyticsUseCase().execute(
                BoardKgAnalyticsCommand(
                    board_id=BOARD_ID,
                    window=AnalyticsUtcWindow(NOW - timedelta(days=90), NOW),
                    as_of=NOW,
                ),
                actor=actor,
                uow=uow,
            )

        kg_payload = kg.data
        assert kg_payload["contract_version"] == "2"
        assert [item["domain"] for item in kg_payload["domains"]] == [
            "active_queue",
            "technical_dlq",
            "canonical_debt",
            "policy_projection_debt",
            "cognitive_backlog",
        ]
        assert (
            CanonicalBoardKgAnalyticsResponseDTO.model_validate(kg_payload).model_dump(
                mode="json", by_alias=True
            )
            == kg_payload
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("graph_probe", ("available", "stale", "unavailable"))
async def test_partial_health_flows_through_real_uow_and_response_contract(
    monkeypatch, graph_probe
):
    from okto_pulse.community.adapters.sqlalchemy_analytics_evidence import (
        CommunitySqlAlchemyBoardKgAnalyticsEvidence,
    )
    from okto_pulse.core.services import kg_health_service

    async def health(board_id, _context):
        assert board_id == BOARD_ID
        return {
            "board_id": board_id,
            "overall_state": "at_risk",
            "graph_state": "at_risk",
            "discovery_state": "healthy",
            "metric_status": "unavailable",
            "classification_reason": "board_graph_metadata_present",
            "board_graph_queryable": True,
            "probe_diagnostics": {
                "graph_snapshot": {"status": graph_probe},
                "graph_metrics": {"status": "available"},
                "discovery_snapshot": {"status": "available"},
                "discovery_telemetry": {"status": "available"},
            },
        }

    async def cognitive(_self, _query, *, observed_at):
        # No access to user file-backed ledgers in this integration fixture.
        return (), None, None

    monkeypatch.setattr(kg_health_service, "get_kg_health", health)
    monkeypatch.setattr(
        CommunitySqlAlchemyBoardKgAnalyticsEvidence, "_cognitive_items", cognitive
    )
    engine = create_async_engine("sqlite+aiosqlite://", future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    register_relational_application_adapter(CommunityRelationalApplicationAdapter())
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            session.add(
                Board(
                    id=BOARD_ID,
                    name="Isolated analytics",
                    owner_id="user-1",
                    realm_id="local",
                )
            )
            await session.commit()
            result = await BoardKgAnalyticsUseCase().execute(
                BoardKgAnalyticsCommand(
                    board_id=BOARD_ID,
                    window=AnalyticsUtcWindow(NOW - timedelta(days=90), NOW),
                    as_of=NOW,
                ),
                actor=RESTAdapterContract.actor("user-1", board_id=BOARD_ID),
                uow=CommunityUnitOfWork(session),
            )
        payload = result.data
        assert (
            CanonicalBoardKgAnalyticsResponseDTO.model_validate(payload).model_dump(
                mode="json", by_alias=True
            )
            == payload
        )
        assert payload["result_state"] == "partial"
        assert payload["health"]["state"] == "at_risk"
        assert payload["health"]["availability"]["discovery"] == "available"
        assert payload["health"]["availability"]["graph"] == (
            "unavailable" if graph_probe == "unavailable" else "partial"
        )
        assert payload["cognitive_inventory"]["total"] == 0
        assert payload["cognitive_inventory"]["result_state"] == "empty"
        diagnostic = next(
            item for item in payload["diagnostics"] if item["domain"] == "health:graph"
        )
        assert (
            diagnostic["next_step"]["target"]
            == f"/api/v1/kg/health?board_id={BOARD_ID}"
        )
        assert diagnostic["severity"] == "at_risk"
    finally:
        await engine.dispose()
