from copy import deepcopy
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.core.ports.historical_archive import ArchiveSection, ArchiveSourceScope
from okto_pulse.core.ports.permission_policy import registered_permission_flags
from okto_pulse.community.adapters.historical_archive_grant_installation import install_historical_archive_grants
from okto_pulse.community.adapters.relational_application import CommunityAgentAuthenticationGateway
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
from okto_pulse.community.adapters.sprint_retirement_archive import capture_sprint_retirement_archive, verify_historical_archive
from okto_pulse.community.adapters.storage import CommunityFileSystemStorage
from test_sprint_retirement_access import add_agent
from test_historical_archive_read import actor, read
import test_sprint_retirement_inventory as relational

database = relational.database
CORE_REPO = Path(os.environ.get("OKTO_PULSE_CORE_REPO", str(Path(__file__).resolve().parents[2] / "okto_labs_pulse_core")))
GOLDEN = json.loads((CORE_REPO / "tests/fixtures/historical_archive_authority_v034.json").read_text(encoding="utf-8"))
SCOPE = ArchiveSourceScope("local", "board-a", "sprint", "sprint")


@pytest.mark.asyncio
async def test_old_full_control_snapshot_captures_installs_and_reads_without_new_authority(database, tmp_path):
    engine, _ = database
    async with engine.begin() as connection:
        await add_agent(connection, "historical-full", flags=GOLDEN["layers"]["full"])
        await add_agent(connection, "historical-denied", flags=GOLDEN["layers"]["full_retired_false"])
        await add_agent(connection, "historical-partial", flags=GOLDEN["layers"]["full_missing_retired"])
    async with AsyncSession(engine) as session:
        gateway = CommunityAgentAuthenticationGateway(session)
        admitted = await gateway.resolve_agent_permission_context("historical-full", board_id="board-a")
        denied = await gateway.resolve_agent_permission_context("historical-denied", board_id="board-a")
        assert not admitted.permissions.owner_review_required
        assert denied.permissions.owner_review_required
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    reference, = await capture_sprint_retirement_archive(engine, storage, migration_id="frozen-policy")
    await install_historical_archive_grants(engine, storage, reference)
    document = await verify_historical_archive(storage, reference)
    by_actor = {grant["actor_id"]: grant["sections"] for grant in document["access"]["grants"]}
    assert all(by_actor["historical-full"].values())
    assert not any(by_actor["historical-denied"].values())
    assert not any(by_actor["historical-partial"].values())
    assert (await read(engine, storage, who=actor("historical-full"))).records()[0]["title"] == "Historical Sprint"
    before = Path(reference.storage_path).read_bytes()
    async with CommunityUnitOfWork(AsyncSession(engine)) as uow:
        await uow.begin_consistent_read()
        await uow.historical_archive_grants.revoke(scope=SCOPE, actor_kind="agent", actor_id="historical-full",
            expected_revision=1, sections=(ArchiveSection.QA,), performed_by_kind="human", performed_by_id="local-user")
        await uow.commit()
    await install_historical_archive_grants(engine, storage, reference)
    async with CommunityUnitOfWork(AsyncSession(engine)) as uow:
        state = await uow.historical_archive_grants.get(scope=SCOPE, actor_kind="agent", actor_id="historical-full")
        assert not state.sections.qa and state.revision == 2
    assert Path(reference.storage_path).read_bytes() == before


@pytest.mark.asyncio
async def test_capture_and_first_install_no_longer_call_live_registry_or_gateway(database, tmp_path, monkeypatch):
    from okto_pulse.core.domain import permissions, sdlc_registry
    from okto_pulse.community import auth
    engine, _ = database
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE boards SET owner_id='local-user' WHERE id='board-a'"))
        await add_agent(connection, "historical-full", flags=GOLDEN["layers"]["full"],
            overrides={"sprint": {"qa": {"read": False}}})
        # Under the original generation this document was partial and denied;
        # today's smaller registry must not reinterpret it as Full Control.
        await add_agent(connection, "ambiguous", flags=registered_permission_flags())
    live = deepcopy(permissions.PERMISSION_REGISTRY)
    assert "sprint" not in live
    monkeypatch.setattr(permissions, "PERMISSION_REGISTRY", live)
    monkeypatch.setattr(permissions, "ALL_FLAGS", [flag for flag in permissions.ALL_FLAGS if not flag.startswith("sprint.")])
    monkeypatch.setattr(sdlc_registry, "SDLC_REGISTRY", {})
    monkeypatch.setattr(auth, "LOCAL_USER", {**auth.LOCAL_USER, "permissions": live})
    async def forbidden(*args, **kwargs):
        raise AssertionError("migration must not use current agent authority")
    monkeypatch.setattr(CommunityAgentAuthenticationGateway, "resolve_agent_permission_context", forbidden)
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    reference, = await capture_sprint_retirement_archive(engine, storage, migration_id="without-live-source-policy")
    await install_historical_archive_grants(engine, storage, reference)
    document = await verify_historical_archive(storage, reference)
    by_actor = {grant["actor_id"]: grant["sections"] for grant in document["access"]["grants"]}
    assert by_actor["historical-full"] == {"content": True, "qa": False, "evaluations": True, "history": True}
    assert all(by_actor["local-user"].values())
    assert not any(by_actor["ambiguous"].values())
