"""P1 adopted population through real relational adapters and canonical lineage.

This internal reader requires an already authorized caller. These tests prove
population/read behavior, not public authorization or Spec start admission.
"""

import pytest
import pytest_asyncio
from sqlalchemy import event, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.sqlalchemy_architecture_persistence import (
    CommunitySqlAlchemyArchitecturePersistence,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    ArchitectureDesign, Base, Board, Ideation, Refinement, Spec,
)
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import (
    CommunitySemanticSession, install_policy_subject_versioning,
)
from okto_pulse.community.adapters.sqlalchemy_resource_gate_service import (
    CommunitySqlAlchemyResourceGateAdapter,
)
from okto_pulse.core.ports.architecture_persistence import (
    register_architecture_persistence_port,
)
from okto_pulse.core.ports.relational_services import register_resource_gate_adapter_factory
from okto_pulse.core.services.architecture_candidates import load_spec_architecture_candidates


@pytest_asyncio.fixture
async def adopted_context(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'candidates.sqlite'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        register_architecture_persistence_port(CommunitySqlAlchemyArchitecturePersistence())
        register_resource_gate_adapter_factory(CommunitySqlAlchemyResourceGateAdapter)
        install_policy_subject_versioning()
        factory = async_sessionmaker(
            engine, expire_on_commit=False, sync_session_class=CommunitySemanticSession,
        )
        async with factory() as db:
            db.add_all([
                Board(id="board", name="Architecture", owner_id="author"),
                Board(id="other-board", name="Private", owner_id="someone-else"),
            ])
            await db.flush()
            db.add(Ideation(id="idea", board_id="board", title="Idea", created_by="author"))
            await db.flush()
            db.add(Refinement(id="refinement", board_id="board", ideation_id="idea",
                              title="Refinement", created_by="author"))
            await db.flush()
            db.add_all([
                Spec(id="spec", board_id="board", refinement_id="refinement",
                     title="Spec", created_by="author", edition=2),
                Spec(id="other-spec", board_id="other-board", title="Private", created_by="someone-else"),
            ])
            await db.commit()
            yield db
    finally:
        await engine.dispose()


def design(design_id, owner_type="spec", owner_id="spec", *, root=None,
           value="adopted", version=1, board_id="board", interfaces=None):
    return ArchitectureDesign(
        id=design_id, board_id=board_id, parent_type=owner_type,
        **{f"{owner_type}_id": owner_id}, title=design_id,
        global_description="Explicitly adopted architecture", created_by="author",
        source_design_id=root, source_version=1 if root else None, version=version,
        interfaces=interfaces if interfaces is not None else [
            {"id": "boundary", "name": "Boundary", "event_schema": {"const": value}},
        ],
    )


async def read(db):
    return await load_spec_architecture_candidates(db, board_id="board", spec_id="spec")


@pytest.mark.asyncio
async def test_nearest_adopted_snapshot_survives_origin_change_and_keeps_other_roots(adopted_context):
    db = adopted_context
    db.add_all([
        design("origin", "ideation", "idea", value="new-origin", version=3),
        design("adopted-copy", "refinement", "refinement", root="origin", value="adopted"),
        design("unrelated", "ideation", "idea", value="independent"),
        design("private", owner_id="other-spec", board_id="other-board", value="secret"),
    ])
    await db.commit()
    before = await read(db)
    assert before.resolved
    by_root = {item.root_design_id: item for item in before.candidates}
    assert set(by_root) == {"origin", "unrelated"}
    assert by_root["origin"].contract["event_schema"] == {"const": "adopted"}
    assert by_root["origin"].adopted_sources == (("adopted-copy", 1),)
    assert by_root["origin"].spec_edition == 2
    await db.execute(update(ArchitectureDesign).where(ArchitectureDesign.id == "origin").values(
        version=4, interfaces=[{"id": "boundary", "event_schema": {"const": "changed-again"}}],
    ))
    await db.commit()
    assert await read(db) == before


@pytest.mark.asyncio
async def test_direct_adoption_shadows_only_its_own_origin(adopted_context):
    db = adopted_context
    db.add_all([
        design("origin", "ideation", "idea", value="original"),
        design("ref-copy", "refinement", "refinement", root="origin", value="refinement"),
        design("spec-copy", root="origin", value="specification"),
        design("unrelated", "refinement", "refinement", value="independent"),
    ])
    await db.commit()
    result = await read(db)
    assert result.resolved
    by_root = {item.root_design_id: item for item in result.candidates}
    assert set(by_root) == {"origin", "unrelated"}
    assert by_root["origin"].adopted_sources == (("spec-copy", 1),)
    assert by_root["origin"].contract["event_schema"] == {"const": "specification"}


@pytest.mark.asyncio
@pytest.mark.parametrize("second_value,expected_count", [("same", 1), ("different", 2)])
async def test_variants_at_same_adopted_owner_keep_provenance_and_conflicts(
    adopted_context, second_value, expected_count,
):
    db = adopted_context
    db.add_all([
        design("copy-a", root="origin", value="same"),
        design("copy-b", root="origin", value=second_value, version=2),
    ])
    await db.commit()
    result = await read(db)
    assert len(result.candidates) == expected_count
    assert len({item.id for item in result.candidates}) == 1
    assert {source for item in result.candidates for source in item.adopted_sources} == {
        ("copy-a", 1), ("copy-b", 2),
    }
    assert result.resolved is (expected_count == 1)
    if expected_count == 2:
        assert [issue.code for issue in result.issues] == ["architecture_contract_revision_conflict"]


@pytest.mark.asyncio
async def test_confirmed_empty_differs_from_incomplete_metadata_population(adopted_context):
    db = adopted_context
    empty = await read(db)
    assert empty.resolved and empty.candidates == ()
    db.add_all([design("one"), design("two")])
    await db.commit()
    register_resource_gate_adapter_factory(
        lambda context: CommunitySqlAlchemyResourceGateAdapter(context, metadata_collection_limit=1)
    )
    unknown = await read(db)
    assert not unknown.source_complete
    assert not unknown.resolved
    assert [issue.code for issue in unknown.issues] == ["architecture_sources_unavailable"]


@pytest.mark.asyncio
async def test_missing_legacy_identity_remains_unresolved_without_writes(adopted_context):
    db = adopted_context
    db.add(design("legacy", interfaces=[{"name": "Legacy", "event_schema": {}}]))
    await db.commit()
    before_spec_version = (await db.get(Spec, "spec")).version
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(db.bind.sync_engine, "before_cursor_execute", capture)
    try:
        first = await read(db)
        assert await read(db) == first
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", capture)
    assert statements and all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    assert not first.resolved
    assert first.issues[0].code == "architecture_contract_identity_required"
    row = await db.get(ArchitectureDesign, "legacy")
    assert row.version == 1 and row.interfaces == [{"name": "Legacy", "event_schema": {}}]
    assert (await db.get(Spec, "spec")).version == before_spec_version


@pytest.mark.asyncio
@pytest.mark.parametrize("spec_id", ["other-spec", "absent-spec"])
async def test_invalid_scope_has_same_safe_error(adopted_context, spec_id):
    with pytest.raises(ValueError, match="^architecture_candidate_scope_unavailable$"):
        await load_spec_architecture_candidates(adopted_context, board_id="board", spec_id=spec_id)


@pytest.mark.asyncio
async def test_malformed_legacy_collection_is_not_confirmed_empty(adopted_context):
    db = adopted_context
    db.add(design("malformed", interfaces={}))
    await db.commit()
    result = await read(db)
    assert not result.source_complete and not result.resolved
    assert result.candidates == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["unavailable", "missing", "revision-changed"])
async def test_payload_failure_or_metadata_revision_mismatch_is_unknown(adopted_context, fault):
    db = adopted_context
    db.add(design("adopted"))
    await db.commit()

    class InterruptedSnapshot(CommunitySqlAlchemyArchitecturePersistence):
        async def list(self, context, query):
            if fault == "unavailable":
                raise RuntimeError("provider message containing private paths and credentials")
            if fault == "missing":
                return ()
            # Fault injection between canonical metadata and payload reads:
            # both are real SQL observations, deliberately different revisions.
            await context.execute(update(ArchitectureDesign).where(
                ArchitectureDesign.id == "adopted",
            ).values(version=2))
            return await super().list(context, query)

    register_architecture_persistence_port(InterruptedSnapshot())
    result = await read(db)
    assert not result.source_complete and not result.resolved
    assert result.candidates == ()
    assert [issue.code for issue in result.issues] == ["architecture_sources_unavailable"]
    assert "private" not in repr(result)
