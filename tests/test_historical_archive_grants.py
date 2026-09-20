"""F2A generic scoped grants on real SQLite, with replay and transaction proofs."""

from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.core.ports.historical_archive import (
    ArchiveGrantConflict, ArchiveSection, ArchiveSourceScope, HistoricalArchiveGrantPort,
)
from okto_pulse.community.adapters.historical_archive_grant_installation import install_historical_archive_grants
from okto_pulse.community.adapters.historical_archive_grants import CommunityHistoricalArchiveGrants
from okto_pulse.community.adapters.sprint_retirement_archive import capture_sprint_retirement_archive
from okto_pulse.community.adapters.sqlalchemy_models import DomainEventRow, HistoricalArchiveGrant, Sprint
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
from okto_pulse.community.adapters.storage import CommunityFileSystemStorage
from test_sprint_retirement_access import add_agent
import test_sprint_retirement_inventory as relational


database = relational.database
SCOPE = ArchiveSourceScope("local", "board-a", "sprint", "sprint")
KEY = {"scope": SCOPE, "actor_kind": "agent", "actor_id": "reader"}


async def capture(database, tmp_path, *, migration="grants"):
    engine, _ = database
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE boards SET owner_id='local-user' WHERE id='board-a'"))
        await add_agent(connection, "reader")
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    reference, = await capture_sprint_retirement_archive(engine, storage, migration_id=migration)
    return storage, reference


async def revoke(adapter, *, revision=1, sections=(ArchiveSection.QA,)):
    return await adapter.revoke(**KEY, expected_revision=revision, sections=sections,
        performed_by_kind="human", performed_by_id="local-user")


@pytest.mark.asyncio
async def test_install_and_replay_preserve_revocation_and_immutable_archive(database, tmp_path):
    engine, _ = database
    storage, reference = await capture(database, tmp_path)
    original = Path(reference.storage_path).read_bytes()
    assert await install_historical_archive_grants(engine, storage, reference) == 2
    async with CommunityUnitOfWork(AsyncSession(engine)) as uow:
        assert isinstance(uow.historical_archive_grants, HistoricalArchiveGrantPort)
        await uow.begin_consistent_read()
        before = await uow.historical_archive_grants.get(**KEY)
        assert before.revision == 1 and before.sections.qa
        after = await revoke(uow.historical_archive_grants)
        assert after.revision == 2 and not after.sections.qa and after.captured.sections.qa
        await uow.commit()
    assert await install_historical_archive_grants(engine, storage, reference) == 2
    async with AsyncSession(engine) as session:
        assert await CommunityHistoricalArchiveGrants(session).get(**KEY) == after
        events = (await session.execute(select(DomainEventRow.event_type))).scalars().all()
        assert events.count("historical_archive.grants_installed") == 1
        assert events.count("historical_archive.sections_revoked") == 1
    assert Path(reference.storage_path).read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"realm_id": "other"}, {"board_id": "board-b"},
    {"origin_kind": "other"}, {"origin_id": "other"}, {"actor_kind": "human"}, {"actor_id": "other"}])
async def test_lookup_requires_every_scope_and_identity_component(database, tmp_path, change):
    engine, _ = database
    storage, reference = await capture(database, tmp_path)
    await install_historical_archive_grants(engine, storage, reference)
    key = dict(KEY)
    scope_changes = {k: v for k, v in change.items() if k not in ("actor_kind", "actor_id")}
    key.update({k: v for k, v in change.items() if k in ("actor_kind", "actor_id")})
    key["scope"] = replace(SCOPE, **scope_changes)
    async with AsyncSession(engine) as session:
        assert await CommunityHistoricalArchiveGrants(session).get(**key) is None


@pytest.mark.asyncio
async def test_outer_rollback_and_audit_failure_both_rollback_authority(database, tmp_path):
    engine, path = database
    storage, reference = await capture(database, tmp_path)
    await install_historical_archive_grants(engine, storage, reference)
    async with CommunityUnitOfWork(AsyncSession(engine)) as uow:
        await uow.begin_consistent_read()
        await revoke(uow.historical_archive_grants)
        # Teardown must undo the savepoint's released change as well.
    async with AsyncSession(engine) as session:
        assert (await CommunityHistoricalArchiveGrants(session).get(**KEY)).sections.qa
    with sqlite3.connect(path) as db:
        db.execute("CREATE TRIGGER deny_archive_revocation_audit BEFORE INSERT ON domain_events "
            "WHEN NEW.event_type='historical_archive.sections_revoked' BEGIN SELECT RAISE(ABORT, 'injected audit failure'); END")
    async with CommunityUnitOfWork(AsyncSession(engine)) as uow:
        await uow.begin_consistent_read()
        from sqlalchemy.exc import IntegrityError
        with pytest.raises(IntegrityError, match="injected audit failure"):
            await revoke(uow.historical_archive_grants)
        assert (await uow.historical_archive_grants.get(**KEY)).revision == 1
        await uow.commit()  # Catching the error must not allow an unaudited revoke.


@pytest.mark.asyncio
async def test_revision_cas_prevents_lost_update_and_noop_does_not_invent_audit(database, tmp_path):
    engine, _ = database
    storage, reference = await capture(database, tmp_path)
    await install_historical_archive_grants(engine, storage, reference)
    async with CommunityUnitOfWork(AsyncSession(engine)) as first:
        await first.begin_consistent_read()
        await revoke(first.historical_archive_grants)
        await first.commit()
    async with CommunityUnitOfWork(AsyncSession(engine)) as second:
        await second.begin_consistent_read()
        with pytest.raises(ArchiveGrantConflict):
            await revoke(second.historical_archive_grants, sections=(ArchiveSection.HISTORY,))
        current = await revoke(second.historical_archive_grants, revision=2)
        assert current.revision == 2 and current.sections.history
        await second.commit()
    async with engine.connect() as connection:
        assert (await connection.execute(text("SELECT count(*) FROM domain_events WHERE event_type='historical_archive.sections_revoked'"))).scalar_one() == 1


@pytest.mark.asyncio
async def test_revocation_requires_physical_outer_transaction_before_savepoint(database, tmp_path):
    engine, _ = database
    storage, reference = await capture(database, tmp_path)
    await install_historical_archive_grants(engine, storage, reference)
    async with AsyncSession(engine) as session:
        with pytest.raises(RuntimeError, match="outer_transaction_required"):
            await revoke(CommunityHistoricalArchiveGrants(session))
        await session.commit()
    async with AsyncSession(engine) as session:
        assert (await CommunityHistoricalArchiveGrants(session).get(**KEY)).sections.qa


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["delete", "captured", "current", "revision"])
async def test_replay_refuses_population_or_provenance_damage_without_regrant(database, tmp_path, damage):
    engine, path = database
    storage, reference = await capture(database, tmp_path)
    await install_historical_archive_grants(engine, storage, reference)
    with sqlite3.connect(path) as db:
        if damage == "delete":
            db.execute("DELETE FROM historical_archive_grants WHERE actor_id='reader'")
        elif damage == "revision":
            db.execute("UPDATE historical_archive_grants SET archive_sha256=? WHERE actor_id='reader'", ("b" * 64,))
        else:
            field = "captured_sections" if damage == "captured" else "sections"
            db.execute(f"UPDATE historical_archive_grants SET {field}=? WHERE actor_id='reader'",
                (json.dumps({"content": True, "qa": False, "evaluations": True, "history": True,
                    **({"unknown": True} if damage == "current" else {})}),))
        before = list(db.iterdump())
    with pytest.raises(ValueError, match="archive_"):
        await install_historical_archive_grants(engine, storage, reference)
    with sqlite3.connect(path) as db:
        assert list(db.iterdump()) == before


@pytest.mark.asyncio
async def test_initial_install_refuses_stale_authority_and_uncommitted_reference(database, tmp_path):
    engine, path = database
    storage, reference = await capture(database, tmp_path)
    with pytest.raises(ValueError, match="committed_reference_required"):
        await install_historical_archive_grants(engine, storage, replace(reference, sha256="b" * 64))
    with sqlite3.connect(path) as db:
        db.execute("UPDATE agents SET is_active=0 WHERE id='reader'")
    with pytest.raises(ValueError, match="authority_changed"):
        await install_historical_archive_grants(engine, storage, reference)
    async with engine.connect() as connection:
        assert (await connection.execute(text("SELECT count(*) FROM historical_archive_grants"))).scalar_one() == 0


@pytest.mark.asyncio
async def test_new_archive_cannot_replace_installed_origin(database, tmp_path):
    engine, _ = database
    storage, reference = await capture(database, tmp_path)
    await install_historical_archive_grants(engine, storage, reference)
    second, = await capture_sprint_retirement_archive(engine, storage, migration_id="different")
    with pytest.raises(ValueError, match="origin_already_installed"):
        await install_historical_archive_grants(engine, storage, second)
    async with AsyncSession(engine) as session:
        assert (await CommunityHistoricalArchiveGrants(session).get(**KEY)).archive_id == reference.event_id


@pytest.mark.asyncio
async def test_create_all_upgrade_is_idempotent_and_does_not_install_authority(database, tmp_path):
    engine, _ = database
    storage, reference = await capture(database, tmp_path)
    async with engine.begin() as connection:
        await connection.run_sync(HistoricalArchiveGrant.__table__.drop)
        await connection.run_sync(HistoricalArchiveGrant.__table__.create)
        await connection.run_sync(lambda sync: HistoricalArchiveGrant.__table__.create(sync, checkfirst=True))
        assert (await connection.execute(text("SELECT count(*) FROM historical_archive_grants"))).scalar_one() == 0
    assert await install_historical_archive_grants(engine, storage, reference) == 2


@pytest.mark.asyncio
async def test_multiple_origins_share_board_but_not_revocation(database, tmp_path):
    engine, _ = database
    async with engine.begin() as connection:
        await connection.execute(insert(Sprint.__table__).values(id="empty", board_id="board-a", spec_id="spec-a",
            title="Empty", created_by="owner"))
    storage, reference = await capture(database, tmp_path)
    assert await install_historical_archive_grants(engine, storage, reference) == 4
    async with CommunityUnitOfWork(AsyncSession(engine)) as uow:
        await uow.begin_consistent_read()
        await revoke(uow.historical_archive_grants, sections=(ArchiveSection.CONTENT,))
        other = await uow.historical_archive_grants.get(**{**KEY, "scope": replace(SCOPE, origin_id="empty")})
        assert other.sections.content and other.revision == 1
        await uow.commit()


@pytest.mark.asyncio
async def test_installation_audit_failure_rolls_back_every_grant_and_can_resume(database, tmp_path):
    engine, path = database
    storage, reference = await capture(database, tmp_path)
    original = Path(reference.storage_path).read_bytes()
    with sqlite3.connect(path) as db:
        db.execute("CREATE TRIGGER fail_archive_install BEFORE INSERT ON domain_events "
            "WHEN NEW.event_type='historical_archive.grants_installed' BEGIN SELECT RAISE(ABORT, 'install audit failure'); END")
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError, match="install audit failure"):
        await install_historical_archive_grants(engine, storage, reference)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM historical_archive_grants").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM domain_events").fetchone()[0] == 1
        db.execute("DROP TRIGGER fail_archive_install")
    assert await install_historical_archive_grants(engine, storage, reference) == 2
    assert Path(reference.storage_path).read_bytes() == original


@pytest.mark.asyncio
async def test_explicit_denials_and_inactive_identity_remain_denied_after_install(database, tmp_path):
    engine, _ = database
    async with engine.begin() as connection:
        await add_agent(connection, "denied", overrides={"sprint": {"qa": {"read": False}}})
        await add_agent(connection, "inactive", active=False)
    storage, reference = await capture(database, tmp_path)
    assert await install_historical_archive_grants(engine, storage, reference) == 4
    async with AsyncSession(engine) as session:
        adapter = CommunityHistoricalArchiveGrants(session)
        denied = await adapter.get(**{**KEY, "actor_id": "denied"})
        inactive = await adapter.get(**{**KEY, "actor_id": "inactive"})
        assert denied.sections.content and not denied.sections.qa
        assert not any(inactive.sections.allows(section) for section in ArchiveSection)
