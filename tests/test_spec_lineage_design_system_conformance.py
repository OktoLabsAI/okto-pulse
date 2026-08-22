"""Community conformance for Design System lineage projections (spec 4f1f3d77).

The tests intentionally exercise the real SQLAlchemy adapter through both public
REST and MCP surfaces.  They prove that catalog pagination stays summary-only,
that item profiles hydrate payloads deliberately, and that effective board
projection distinguishes configuration from resolution (including a dangling
legacy/corrupt link).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from okto_pulse.community.adapters.sqlalchemy_base import Base
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import (
    CommunitySemanticSession,
)
from okto_pulse.community.adapters.sqlalchemy_architecture_persistence import (
    CommunitySqlAlchemyArchitecturePersistence,
)
from okto_pulse.community.adapters.sqlalchemy_database import (
    install_community_sqlite_pragmas,
)
from okto_pulse.community.adapters.sqlalchemy_design_system import (
    CommunitySqlAlchemyDesignSystemStore,
)
from okto_pulse.community.adapters.sqlalchemy_domain_event_delivery import (
    CommunitySqlAlchemyDomainEventPublisher,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    ArchitectureDesign,
    Board,
    Ideation,
    Refinement,
)
from okto_pulse.community.adapters.sqlalchemy_spec_resource_propagation import (
    CommunitySqlAlchemySpecResourcePropagationStore,
)
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import (
    CommunityUnitOfWorkFactory,
)
from okto_pulse.community.api.auth_deps import require_principal, require_user
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.api.design_systems import router as design_system_router
from okto_pulse.community.api.refinements import router as refinement_router
from okto_pulse.core.domain.enums import IdeationStatus
from okto_pulse.core.domain.permissions import get_builtin_presets
from okto_pulse.core.mcp import server as mcp_server
from okto_pulse.core.ports.architecture_persistence import (
    register_architecture_persistence_port,
)
from okto_pulse.core.ports.authentication import Principal
from okto_pulse.core.ports.design_system import (
    DesignSystemRecord,
    register_design_system_store,
)
from okto_pulse.core.ports.domain_event_delivery import (
    register_domain_event_publisher,
)
from okto_pulse.core.ports.spec_resource_propagation import (
    register_spec_resource_propagation_store,
)
from okto_pulse.core.runtime_registry import register_unit_of_work_factory


BOARD_ID = "ts-9c7f3ee0-board"
ACTOR_ID = "ts-9c7f3ee0-agent"
NOW = datetime(2026, 7, 23, 22, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class _Runtime:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    store: CommunitySqlAlchemyDesignSystemStore
    uow_factory: CommunityUnitOfWorkFactory


@pytest.fixture
async def design_system_runtime(tmp_path) -> _Runtime:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'lineage-design-system.db'}"
    )
    install_community_sqlite_pragmas(engine)
    sessions = async_sessionmaker(
        engine,
        class_=AsyncSession,
        sync_session_class=CommunitySemanticSession,
        expire_on_commit=False,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    store = CommunitySqlAlchemyDesignSystemStore()
    uow_factory = CommunityUnitOfWorkFactory(sessions)
    register_design_system_store(store)
    register_unit_of_work_factory(uow_factory)
    register_architecture_persistence_port(
        CommunitySqlAlchemyArchitecturePersistence()
    )
    register_domain_event_publisher(CommunitySqlAlchemyDomainEventPublisher())
    register_spec_resource_propagation_store(
        CommunitySqlAlchemySpecResourcePropagationStore()
    )

    async with sessions() as session:
        session.add(
            Board(
                id=BOARD_ID,
                name="Design System lineage conformance",
                owner_id=ACTOR_ID,
                realm_id="local",
                settings={"design_system_gate_mode": "blocking"},
            )
        )
        await session.commit()

    runtime = _Runtime(
        engine=engine,
        sessions=sessions,
        store=store,
        uow_factory=uow_factory,
    )
    try:
        yield runtime
    finally:
        await engine.dispose()


def _record(
    design_system_id: str,
    title: str,
    *,
    payload: dict[str, Any] | None,
) -> DesignSystemRecord:
    return DesignSystemRecord(
        id=design_system_id,
        scope="global",
        board_id=None,
        title=title,
        payload=payload,
        version=1,
        status="active",
        owner_id=ACTOR_ID,
        created_at=NOW,
        updated_at=NOW,
    )


async def _seed_catalog(runtime: _Runtime) -> tuple[DesignSystemRecord, ...]:
    records = (
        _record("ds-c", "C catalog", payload={"tokens": "c"}),
        _record("ds-a", "A catalog", payload={"tokens": "x" * 12_000}),
        _record("ds-b", "B catalog", payload=None),
    )
    async with runtime.sessions() as session:
        for record in records:
            await runtime.store.create(session, record)
        await session.commit()
    return records


def _rest_app(runtime: _Runtime) -> FastAPI:
    app = FastAPI()
    app.include_router(design_system_router)
    app.include_router(refinement_router)

    async def _uow():
        async with runtime.uow_factory() as uow:
            yield uow

    app.dependency_overrides[get_unit_of_work] = _uow
    app.dependency_overrides[require_user] = lambda: ACTOR_ID
    full_control = next(
        preset["flags"]
        for preset in get_builtin_presets()
        if preset["name"] == "Full Control"
    )
    app.dependency_overrides[require_principal] = lambda: Principal(
        subject=ACTOR_ID,
        realm_id="local",
        claims={"roles": ["admin"], "permissions": full_control},
        actor_kind="human",
    )
    return app


def _install_mcp_actor(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _agent_context(board_id: str):
        return SimpleNamespace(
            agent_id=ACTOR_ID,
            agent_name="Design System conformance",
            permissions=None,
            realm_id="local",
            board_id=board_id,
        )

    monkeypatch.setattr(mcp_server, "_get_agent_ctx", _agent_context)


async def _force_dangling_board_link(runtime: _Runtime) -> None:
    """Simulate a legacy/corrupt link without weakening normal runtime FK checks."""

    async with runtime.engine.connect() as connection:
        await connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        await connection.exec_driver_sql(
            "UPDATE board_design_systems "
            "SET design_system_id = ? WHERE board_id = ?",
            ("missing-design-system", BOARD_ID),
        )
        await connection.commit()
        await connection.exec_driver_sql("PRAGMA foreign_keys=ON")


@pytest.mark.asyncio
async def test_community_sqlalchemy_design_system_store_is_stable_and_bounded(
    design_system_runtime: _Runtime,
) -> None:
    runtime = design_system_runtime
    await _seed_catalog(runtime)

    async with runtime.sessions() as session:
        first = await runtime.store.list_catalog(
            session,
            scope="global",
            board_id=None,
            limit=2,
            offset=0,
        )
        second = await runtime.store.list_catalog(
            session,
            scope="global",
            board_id=None,
            limit=2,
            offset=2,
        )
        link = await runtime.store.upsert_board_link(
            session,
            board_id=BOARD_ID,
            design_system_id="ds-a",
            design_system_version=1,
        )
        await session.commit()

    assert [item.id for item in first] == ["ds-a", "ds-b"]
    assert [item.id for item in second] == ["ds-c"]
    assert first[0].payload == {"tokens": "x" * 12_000}
    assert link.design_system_id == "ds-a"

    async with runtime.sessions() as session:
        persisted = await runtime.store.get_board_link(session, board_id=BOARD_ID)
    assert persisted == link


@pytest.mark.asyncio
async def test_ts_9c7f3ee0_rest_mcp_profiles_and_effective_link_conformance(
    design_system_runtime: _Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ts_9c7f3ee0: SQL + REST + MCP parity for AC-5/AC-6."""

    runtime = design_system_runtime
    await _seed_catalog(runtime)
    _install_mcp_actor(monkeypatch)
    app = _rest_app(runtime)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://community.test",
    ) as client:
        linked = await client.post(
            f"/boards/{BOARD_ID}/design-system",
            json={"design_system_id": "ds-a"},
        )
        assert linked.status_code == 200

        page_one = await client.get(
            "/design-systems",
            params={
                "scope": "global",
                "board_id": BOARD_ID,
                "limit": 2,
                "profile": "summary",
            },
        )
        assert page_one.status_code == 200
        first_page = page_one.json()
        assert first_page["profile"] == "summary"
        assert first_page["count"] == 2
        assert first_page["next_cursor"]
        assert all("payload" not in item for item in first_page["items"])
        assert len(page_one.content) < 5_000

        page_two = await client.get(
            "/design-systems",
            params={
                "scope": "global",
                "board_id": BOARD_ID,
                "limit": 2,
                "cursor": first_page["next_cursor"],
                "profile": "summary",
            },
        )
        assert page_two.status_code == 200
        assert {
            item["id"] for item in first_page["items"] + page_two.json()["items"]
        } == {"ds-a", "ds-b", "ds-c"}

        invalid_list_profile = await client.get(
            "/design-systems",
            params={
                "scope": "global",
                "board_id": BOARD_ID,
                "profile": "full",
            },
        )
        assert invalid_list_profile.status_code == 422
        assert (
            invalid_list_profile.json()["detail"]["code"]
            == "design_system_invalid_profile"
        )

        for profile in ("summary", "detail", "full"):
            response = await client.get(
                "/design-systems/ds-a",
                params={"board_id": BOARD_ID, "profile": profile},
            )
            assert response.status_code == 200
            item = response.json()
            if profile == "summary":
                assert "payload" not in item
                assert item["payload_available"] is True
            else:
                assert item["payload"] == {"tokens": "x" * 12_000}

        effective_response = await client.get(
            f"/boards/{BOARD_ID}/design-system"
        )
        assert effective_response.status_code == 200
        effective = effective_response.json()["effective"]
        assert effective["configured"] is True
        assert effective["resolvable"] is True
        assert effective["mandate"] is True
        assert effective["exists"] is True

    mcp_list = json.loads(
        await mcp_server.okto_pulse_list_design_systems.fn(
            board_id=BOARD_ID,
            scope="global",
            limit=2,
            profile="summary",
        )
    )
    assert mcp_list["profile"] == "summary"
    assert all("payload" not in item for item in mcp_list["items"])
    mcp_invalid_list_profile = json.loads(
        await mcp_server.okto_pulse_list_design_systems.fn(
            board_id=BOARD_ID,
            scope="global",
            profile="detail",
        )
    )
    assert mcp_invalid_list_profile["code"] == "design_system_invalid_profile"

    for profile in ("summary", "detail", "full"):
        item = json.loads(
            await mcp_server.okto_pulse_get_design_system.fn(
                board_id=BOARD_ID,
                design_system_id="ds-a",
                profile=profile,
            )
        )
        assert ("payload" in item) is (profile != "summary")

    mcp_effective = json.loads(
        await mcp_server.okto_pulse_get_board_design_system.fn(board_id=BOARD_ID)
    )["effective"]
    assert {
        key: mcp_effective[key]
        for key in ("configured", "resolvable", "mandate", "exists")
    } == {
        "configured": True,
        "resolvable": True,
        "mandate": True,
        "exists": True,
    }

    await _force_dangling_board_link(runtime)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://community.test",
    ) as client:
        dangling_response = await client.get(
            f"/boards/{BOARD_ID}/design-system"
        )
    assert dangling_response.status_code == 200
    dangling = dangling_response.json()["effective"]
    assert dangling["configured"] is True
    assert dangling["resolvable"] is False
    assert dangling["mandate"] is True
    assert dangling["exists"] is False
    assert dangling["design_system_id"] == "missing-design-system"

    mcp_dangling = json.loads(
        await mcp_server.okto_pulse_get_board_design_system.fn(board_id=BOARD_ID)
    )["effective"]
    assert mcp_dangling == dangling


@pytest.mark.asyncio
async def test_rest_mcp_architecture_selection_error_parity_is_atomic(
    design_system_runtime: _Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = design_system_runtime
    ideation_id = "architecture-selection-ideation"
    root_design_id = "architecture-selection-root"
    missing_design_id = "architecture-selection-missing"
    async with runtime.sessions() as session:
        session.add(
            Ideation(
                id=ideation_id,
                board_id=BOARD_ID,
                title="Architecture selection source",
                status=IdeationStatus.DONE,
                created_by=ACTOR_ID,
            )
        )
        session.add(
            ArchitectureDesign(
                id=root_design_id,
                board_id=BOARD_ID,
                parent_type="ideation",
                ideation_id=ideation_id,
                title="Canonical root",
                global_description="Selection parity fixture.",
                entities=[],
                interfaces=[],
                diagrams=[],
                created_by=ACTOR_ID,
            )
        )
        await session.commit()

    _install_mcp_actor(monkeypatch)
    app = _rest_app(runtime)
    request_payload = {
        "ideation_id": ideation_id,
        "title": "Must not persist",
        "in_scope": ["Prove fail-closed selection"],
        "delivery_context": "greenfield",
        "architecture_design_ids": [root_design_id, missing_design_id],
        "architecture_propagation_mode": "copy",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://community.test",
    ) as client:
        rest_response = await client.post(
            f"/ideations/{ideation_id}/refinements",
            json=request_payload,
        )

    assert rest_response.status_code == 422
    rest_error = rest_response.json()["detail"]
    mcp_error = json.loads(
        await mcp_server.okto_pulse_create_refinement.fn(
            board_id=BOARD_ID,
            ideation_id=ideation_id,
            title="Must not persist through MCP",
            in_scope=["Prove fail-closed selection"],
            delivery_context="greenfield",
            architecture_design_ids=[root_design_id, missing_design_id],
            architecture_propagation_mode="copy",
        )
    )
    for field in (
        "error",
        "code",
        "source_parent_type",
        "source_parent_id",
        "requested",
        "matched",
        "missing",
        "retryable",
    ):
        assert rest_error[field] == mcp_error[field]
    assert rest_error["code"] == "architecture_design_selection_invalid"

    async with runtime.sessions() as session:
        rows = (
            await session.execute(
                Refinement.__table__.select().where(
                    Refinement.ideation_id == ideation_id
                )
            )
        ).all()
    assert rows == []
