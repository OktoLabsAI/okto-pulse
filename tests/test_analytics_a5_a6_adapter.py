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
    Card,
    Spec,
    Sprint,
    SprintActivationBaseline,
)
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import (
    CommunitySemanticSession,
)
from okto_pulse.community.api.analytics_transport import (
    CanonicalBoardKgAnalyticsResponseDTO,
    CanonicalDeliveryForecastResponseDTO,
)
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.core.application.use_cases.board_kg_analytics import (
    BoardKgAnalyticsCommand,
    BoardKgAnalyticsUseCase,
)
from okto_pulse.core.application.use_cases.delivery_forecast import (
    DeliveryForecastCommand,
    DeliveryForecastUseCase,
)
from okto_pulse.core.domain.enums import CardStatus, SprintStatus
from okto_pulse.core.ports.analytics_foundation import AnalyticsUtcWindow
from okto_pulse.core.ports.relational_application import (
    RelationalApplicationAdapter,
    register_relational_application_adapter,
)


NOW = datetime(2026, 8, 21, 12, tzinfo=UTC)
BOARD_ID = "11111111-1111-4111-8111-111111111111"
SPEC_ID = "22222222-2222-4222-8222-222222222222"


@pytest.mark.asyncio
async def test_a5_a6_are_reachable_through_real_uow_and_relational_adapter() -> None:
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
            session.add(
                Spec(
                    id=SPEC_ID,
                    board_id=BOARD_ID,
                    title="Forecast authority",
                    created_by="user-1",
                )
            )
            for index in range(8):
                sprint_id = f"33333333-3333-4333-8{index:03d}-{index:012d}"
                card_id = f"44444444-4444-4444-8{index:03d}-{index:012d}"
                completed_at = NOW - timedelta(days=(8 - index) * 7)
                session.add(
                    Sprint(
                        id=sprint_id,
                        board_id=BOARD_ID,
                        spec_id=SPEC_ID,
                        title=f"Sprint {index}",
                        status=SprintStatus.CLOSED,
                        created_by="user-1",
                        created_at=completed_at - timedelta(days=7),
                        updated_at=completed_at,
                    )
                )
                session.add(
                    Card(
                        id=card_id,
                        board_id=BOARD_ID,
                        spec_id=SPEC_ID,
                        sprint_id=sprint_id,
                        title=f"Delivered {index}",
                        status=CardStatus.DONE,
                        created_by="user-1",
                        created_at=completed_at - timedelta(days=6),
                        updated_at=completed_at,
                    )
                )
                session.add(
                    SprintActivationBaseline(
                        baseline_ref=f"baseline:{index}",
                        board_id=BOARD_ID,
                        sprint_id=sprint_id,
                        spec_id=SPEC_ID,
                        sprint_version=1,
                        activated_at=completed_at - timedelta(days=7),
                        activated_by="user-1",
                        member_count=1,
                        members=[
                            {
                                "card_id": card_id,
                                "card_type": "normal",
                                "card_version": 1,
                            }
                        ],
                    )
                )
            await session.commit()

        actor = RESTAdapterContract.actor("user-1", board_id=BOARD_ID)
        async with factory() as session:
            uow = CommunityUnitOfWork(session)
            forecast = await DeliveryForecastUseCase().execute(
                DeliveryForecastCommand(
                    board_id=BOARD_ID,
                    window=AnalyticsUtcWindow(NOW - timedelta(days=90), NOW),
                    as_of=NOW,
                ),
                actor=actor,
                uow=uow,
            )
            kg = await BoardKgAnalyticsUseCase().execute(
                BoardKgAnalyticsCommand(
                    board_id=BOARD_ID,
                    window=AnalyticsUtcWindow(NOW - timedelta(days=90), NOW),
                    as_of=NOW,
                ),
                actor=actor,
                uow=uow,
            )

        forecast_payload = forecast.data
        assert forecast_payload["readiness"]["state"] == "ready"
        assert forecast_payload["forecast"]["sample_size"] == 8
        assert forecast_payload["forecast"]["point"] == 1.0
        assert (
            CanonicalDeliveryForecastResponseDTO.model_validate(forecast_payload)
            .model_dump(mode="json", by_alias=True)
            == forecast_payload
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
            CanonicalBoardKgAnalyticsResponseDTO.model_validate(kg_payload)
            .model_dump(mode="json", by_alias=True)
            == kg_payload
        )
    finally:
        await engine.dispose()
