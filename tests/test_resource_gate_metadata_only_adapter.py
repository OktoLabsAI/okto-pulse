"""Community Resource Gate gate-profile reads stay metadata-only."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os
import re

import pytest
from sqlalchemy import event

import okto_pulse.community.app as _community_app  # noqa: F401
import okto_pulse.core.infra.database as _db_mod
from okto_pulse.community.adapters.relational_schema_lifecycle import (
    register_community_relational_schema_lifecycle,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    ArchitectureDesign,
    ArchitectureFindingRun,
    Board,
    Card,
    KnowledgeAssignmentRecord,
    KnowledgePropagationScopeRecord,
    KnowledgeSnapshotRecord,
    KnowledgeTombstoneRecord,
    Spec,
    SpecKnowledgeBase,
)
from okto_pulse.community.adapters.sqlalchemy_resource_gate_service import (
    CommunitySqlAlchemyResourceGateAdapter,
)
from okto_pulse.core.ports.relational_services import (
    register_resource_gate_adapter_factory,
)
from okto_pulse.core.services.resource_gate import ResourceGateService
from okto_pulse.core.services.resource_gate_contracts import ResourceGateError


@pytest.fixture
def metadata_session_factory(tmp_path):
    import okto_pulse.core.infra.config as _config
    from okto_pulse.community.config import CommunitySettings

    saved_data = os.environ.get("DATA_DIR")
    saved_kg = os.environ.get("KG_BASE_DIR")
    os.environ["DATA_DIR"] = str(tmp_path)
    os.environ["KG_BASE_DIR"] = str(tmp_path / "boards")
    _config.configure_settings(CommunitySettings())

    async def setup() -> None:
        _db_mod.create_database(
            f"sqlite+aiosqlite:///{tmp_path / 'resource-gate-metadata.db'}"
        )
        register_community_relational_schema_lifecycle()
        await _db_mod.init_db()

    asyncio.run(setup())
    try:
        yield _db_mod.get_session_factory()
    finally:
        asyncio.run(_db_mod.close_db())
        for key, value in (("DATA_DIR", saved_data), ("KG_BASE_DIR", saved_kg)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_gate_profile_projects_only_persisted_metadata(
    metadata_session_factory,
) -> None:
    register_resource_gate_adapter_factory(CommunitySqlAlchemyResourceGateAdapter)
    body = "sensitive-body-" + ("x" * 200_000)
    persisted_hash = "a" * 64

    async def drive() -> tuple[dict, list[str], list[object]]:
        async with metadata_session_factory() as db:
            db.add(Board(id="board-1", name="Board", owner_id="owner", settings={}))
            db.add(
                Spec(
                    id="spec-1",
                    board_id="board-1",
                    title="Spec",
                    description=body,
                    context=body,
                    screen_mockups=[
                        {
                            "id": "mockup-1",
                            "title": "Current UI",
                            "description": body,
                            "html_content": body,
                            "root_source_mockup_id": "mockup-root",
                        }
                    ],
                    created_by="owner",
                )
            )
            db.add(
                SpecKnowledgeBase(
                    id="kb-1",
                    spec_id="spec-1",
                    title="Runbook",
                    description=body,
                    content=body,
                    source_version=7,
                    root_source_kb_id="kb-root",
                    immediate_parent_kb_id="kb-parent",
                    source_kb_id="kb-parent",
                    content_hash=persisted_hash,
                    created_by="owner",
                )
            )
            db.add(
                ArchitectureDesign(
                    id="arch-1",
                    board_id="board-1",
                    parent_type="spec",
                    spec_id="spec-1",
                    title="Architecture",
                    global_description=body,
                    entities=[{"description": body}],
                    interfaces=[{"description": body}],
                    diagrams=[{"content": body}],
                    version=5,
                    source_version=3,
                    created_by="owner",
                )
            )
            await db.flush()
            db.add(
                ArchitectureFindingRun(
                    id="finding-run-1",
                    board_id="board-1",
                    design_id="arch-1",
                    design_version=5,
                    critic_run_id="critic-1",
                    is_current=True,
                    active_count=0,
                    resolved_count=2,
                    superseded_count=1,
                    validator_summary={
                        "valid": True,
                        "issues": [],
                        "private_body": body,
                    },
                    actor_id="owner",
                )
            )
            await db.commit()

        statements: list[str] = []

        async with metadata_session_factory() as db:
            sync_engine = db.get_bind().engine

            def capture(
                _conn: object,
                _cursor: object,
                statement: str,
                _parameters: object,
                _context: object,
                _executemany: bool,
            ) -> None:
                statements.append(statement)

            event.listen(sync_engine, "before_cursor_execute", capture)
            try:
                service = ResourceGateService(db)
                lineage = await service._resolve_resource_lineage(
                    "board-1",
                    "spec",
                    "spec-1",
                    include_coverage=False,
                    projection_profile="gate",
                )
                summary = lineage.to_dict()
                hydrated = list(db.identity_map.values())
            finally:
                event.remove(sync_engine, "before_cursor_execute", capture)
        return summary, statements, hydrated

    summary, statements, hydrated = asyncio.run(drive())

    resources = {
        item["resource_type"]: item for item in summary["resource_states"]
    }
    assert resources["architecture"]["state"] == "provided"
    assert resources["mockup"]["state"] == "provided"
    assert resources["knowledge_base"]["state"] == "provided"
    knowledge_ref = resources["knowledge_base"]["direct_refs"][0]
    assert knowledge_ref["source_revision"] == 7
    assert knowledge_ref["source_content_sha256"] == persisted_hash
    architecture_ref = resources["architecture"]["direct_refs"][0]
    assert architecture_ref["design_version"] == 5
    assert architecture_ref["current_finding_run"] == {
        "critic_run_id": "critic-1",
        "design_version": 5,
        "is_current": True,
        "active_count": 0,
        "resolved_count": 2,
        "superseded_count": 1,
        "validator_valid": True,
        "validator_issue_count": 0,
    }

    serialized = repr(summary)
    assert "sensitive-body" not in serialized

    def assert_no_body_fields(value: object) -> None:
        if isinstance(value, dict):
            assert not {
                "description",
                "content",
                "html_content",
                "global_description",
                "entities",
                "interfaces",
                "diagrams",
                "validator_summary",
            } & set(value)
            for child in value.values():
                assert_no_body_fields(child)
        elif isinstance(value, list):
            for child in value:
                assert_no_body_fields(child)

    assert_no_body_fields(summary)
    assert not any(
        isinstance(item, (Spec, SpecKnowledgeBase, ArchitectureDesign))
        for item in hydrated
    )

    sql = "\n".join(statements).lower()
    for qualified_column in (
        "spec_knowledge_bases.description",
        "spec_knowledge_bases.content",
        "architecture_designs.global_description",
        "architecture_designs.entities",
        "architecture_designs.interfaces",
        "architecture_designs.diagrams",
    ):
        assert re.search(
            rf"{re.escape(qualified_column)}(?:\s|,)", sql
        ) is None


def test_missing_persisted_hash_is_not_computed_from_content(
    metadata_session_factory,
) -> None:
    async def drive() -> dict:
        async with metadata_session_factory() as db:
            db.add(Board(id="board-2", name="Board", owner_id="owner", settings={}))
            db.add(
                Spec(
                    id="spec-2",
                    board_id="board-2",
                    title="Spec",
                    created_by="owner",
                )
            )
            db.add(
                SpecKnowledgeBase(
                    id="kb-no-hash",
                    spec_id="spec-2",
                    title="Legacy",
                    content="must-not-be-hashed" * 10_000,
                    content_hash=None,
                    created_by="owner",
                )
            )
            await db.commit()
        async with metadata_session_factory() as db:
            adapter = CommunitySqlAlchemyResourceGateAdapter(db)
            ref = await adapter.load_entity_ref_metadata(
                "board-2", "spec", "spec-2"
            )
            return (await adapter.collect_refs_metadata(ref))["knowledge_base"][0]

    knowledge_ref = asyncio.run(drive())
    assert knowledge_ref.get("content_hash") is None
    assert knowledge_ref["source_content_sha256"] is None


def test_gate_profile_filters_v2_snapshot_from_persisted_stamps_only(
    metadata_session_factory,
) -> None:
    register_resource_gate_adapter_factory(CommunitySqlAlchemyResourceGateAdapter)
    occurred_at = datetime.now(timezone.utc)
    source_hash = "b" * 64
    snapshot_hash = "c" * 64

    async def drive() -> tuple[dict, list[str]]:
        async with metadata_session_factory() as db:
            db.add(Board(id="board-3", name="Board", owner_id="owner", settings={}))
            db.add(
                Spec(
                    id="spec-3",
                    board_id="board-3",
                    title="Spec",
                    created_by="owner",
                )
            )
            db.add(
                Card(
                    id="card-3",
                    board_id="board-3",
                    spec_id="spec-3",
                    title="Task",
                    created_by="owner",
                )
            )
            db.add(
                SpecKnowledgeBase(
                    id="kb-source",
                    spec_id="spec-3",
                    title="Source",
                    content="source-body" * 20_000,
                    source_version=4,
                    root_source_kb_id="kb-root",
                    content_hash=source_hash,
                    created_by="owner",
                )
            )
            await db.flush()
            db.add(
                KnowledgePropagationScopeRecord(
                    id="scope-card-3",
                    board_id="board-3",
                    target_type="card",
                    target_id="card-3",
                    scope_revision=1,
                    v2_active=True,
                    selection_state="explicit_ids",
                    v2_activated_at=occurred_at,
                )
            )
            await db.flush()
            db.add(
                KnowledgeAssignmentRecord(
                    assignment_id="assignment-card-3",
                    scope_id="scope-card-3",
                    source_knowledge_id="kb-source",
                    root_id="kb-root",
                    immediate_parent_id="kb-source",
                    source_revision="4",
                    source_content_sha256=source_hash,
                    mode="snapshot",
                    state="active",
                    origin_class="v2",
                    actor_id="owner",
                    revision=1,
                    justification="freeze for task",
                    relevance_links=[],
                    effective_from=occurred_at,
                )
            )
            await db.flush()
            db.add(
                KnowledgeSnapshotRecord(
                    snapshot_id="snapshot-card-3",
                    scope_id="scope-card-3",
                    assignment_id="assignment-card-3",
                    root_id="kb-root",
                    immediate_parent_id="kb-source",
                    source_revision="4",
                    source_content_sha256=snapshot_hash,
                    content_bytes=(b"snapshot-body" * 20_000),
                    effective_from=occurred_at,
                )
            )
            await db.commit()

        statements: list[str] = []
        async with metadata_session_factory() as db:
            sync_engine = db.get_bind().engine

            def capture(
                _conn: object,
                _cursor: object,
                statement: str,
                _parameters: object,
                _context: object,
                _executemany: bool,
            ) -> None:
                statements.append(statement)

            event.listen(sync_engine, "before_cursor_execute", capture)
            try:
                service = ResourceGateService(db)
                lineage = await service._resolve_resource_lineage(
                    "board-3",
                    "card",
                    "card-3",
                    include_coverage=False,
                    projection_profile="gate",
                )
            finally:
                event.remove(sync_engine, "before_cursor_execute", capture)
        return lineage.to_dict(), statements

    lineage, statements = asyncio.run(drive())
    knowledge = next(
        item
        for item in lineage["resource_states"]
        if item["resource_type"] == "knowledge_base"
    )
    assert knowledge["state"] == "provided"
    assert knowledge["direct_count"] == 0
    assert knowledge["inherited_count"] == 1
    inherited = knowledge["inherited_refs"][0]
    assert inherited["source_content_sha256"] == snapshot_hash
    assert inherited["root_resource_id"] == "kb-root"
    assert "source-body" not in repr(lineage)
    assert "snapshot-body" not in repr(lineage)

    sql = "\n".join(statements).lower()
    for qualified_column in (
        "spec_knowledge_bases.description",
        "spec_knowledge_bases.content",
        "knowledge_propagation_snapshots.content_bytes",
    ):
        assert re.search(
            rf"{re.escape(qualified_column)}(?:\s|,)", sql
        ) is None


def test_every_metadata_collection_fails_closed_on_limit_plus_one(
    metadata_session_factory,
) -> None:
    occurred_at = datetime.now(timezone.utc)
    source_hash = "d" * 64

    async def drive() -> list[ResourceGateError]:
        async with metadata_session_factory() as db:
            db.add(
                Board(
                    id="overflow-board",
                    name="Overflow",
                    owner_id="owner",
                    settings={},
                )
            )
            db.add_all(
                [
                    Spec(
                        id="overflow-architecture",
                        board_id="overflow-board",
                        title="Architecture owner",
                        created_by="owner",
                    ),
                    Spec(
                        id="overflow-mockup",
                        board_id="overflow-board",
                        title="Mockup owner",
                        screen_mockups=[
                            {"id": "mockup-a", "title": "A", "content": "body-a"},
                            {"id": "mockup-b", "title": "B", "content": "body-b"},
                        ],
                        created_by="owner",
                    ),
                    Spec(
                        id="overflow-finding-run",
                        board_id="overflow-board",
                        title="Finding run owner",
                        created_by="owner",
                    ),
                    Spec(
                        id="overflow-kb-row",
                        board_id="overflow-board",
                        title="KB owner",
                        created_by="owner",
                    ),
                    Card(
                        id="overflow-card-json",
                        board_id="overflow-board",
                        title="Card JSON owner",
                        knowledge_bases=[
                            {"id": "card-kb-a", "title": "A", "content": "body-a"},
                            {"id": "card-kb-b", "title": "B", "content": "body-b"},
                        ],
                        created_by="owner",
                    ),
                    Card(
                        id="overflow-assignment",
                        board_id="overflow-board",
                        title="Assignment owner",
                        created_by="owner",
                    ),
                    Card(
                        id="overflow-tombstone",
                        board_id="overflow-board",
                        title="Tombstone owner",
                        created_by="owner",
                    ),
                ]
            )
            db.add_all(
                [
                    ArchitectureDesign(
                        id="overflow-arch-a",
                        board_id="overflow-board",
                        parent_type="spec",
                        spec_id="overflow-architecture",
                        title="A",
                        global_description="body-a",
                        entities=[],
                        interfaces=[],
                        diagrams=[],
                        created_by="owner",
                    ),
                    ArchitectureDesign(
                        id="overflow-arch-b",
                        board_id="overflow-board",
                        parent_type="spec",
                        spec_id="overflow-architecture",
                        title="B",
                        global_description="body-b",
                        entities=[],
                        interfaces=[],
                        diagrams=[],
                        created_by="owner",
                    ),
                    ArchitectureDesign(
                        id="overflow-finding-design",
                        board_id="overflow-board",
                        parent_type="spec",
                        spec_id="overflow-finding-run",
                        title="Finding design",
                        global_description="body",
                        entities=[],
                        interfaces=[],
                        diagrams=[],
                        created_by="owner",
                    ),
                    SpecKnowledgeBase(
                        id="overflow-kb-a",
                        spec_id="overflow-kb-row",
                        title="A",
                        content="body-a",
                        created_by="owner",
                    ),
                    SpecKnowledgeBase(
                        id="overflow-kb-b",
                        spec_id="overflow-kb-row",
                        title="B",
                        content="body-b",
                        created_by="owner",
                    ),
                ]
            )
            await db.flush()
            db.add_all(
                [
                    ArchitectureFindingRun(
                        id=f"overflow-finding-run-{suffix}",
                        board_id="overflow-board",
                        design_id="overflow-finding-design",
                        design_version=1,
                        critic_run_id=f"critic-{suffix}",
                        is_current=True,
                        active_count=0,
                        resolved_count=0,
                        superseded_count=0,
                        validator_summary={"valid": True, "issues": []},
                        actor_id="owner",
                    )
                    for suffix in ("a", "b")
                ]
            )
            db.add_all(
                [
                    KnowledgePropagationScopeRecord(
                        id="overflow-assignment-scope",
                        board_id="overflow-board",
                        target_type="card",
                        target_id="overflow-assignment",
                        scope_revision=1,
                        v2_active=True,
                        selection_state="explicit_ids",
                        v2_activated_at=occurred_at,
                    ),
                    KnowledgePropagationScopeRecord(
                        id="overflow-tombstone-scope",
                        board_id="overflow-board",
                        target_type="card",
                        target_id="overflow-tombstone",
                        scope_revision=1,
                        v2_active=True,
                        selection_state="explicit_empty",
                        v2_activated_at=occurred_at,
                    ),
                ]
            )
            await db.flush()
            db.add_all(
                [
                    KnowledgeAssignmentRecord(
                        assignment_id=f"overflow-assignment-{suffix}",
                        scope_id="overflow-assignment-scope",
                        source_knowledge_id=f"source-{suffix}",
                        root_id=f"root-{suffix}",
                        source_revision="1",
                        source_content_sha256=source_hash,
                        mode="reference",
                        state="active",
                        origin_class="v2",
                        actor_id="owner",
                        revision=1,
                        justification="overflow fixture",
                        relevance_links=[],
                        effective_from=occurred_at,
                    )
                    for suffix in ("a", "b")
                ]
            )
            db.add_all(
                [
                    KnowledgeTombstoneRecord(
                        tombstone_id=f"overflow-tombstone-{suffix}",
                        scope_id="overflow-tombstone-scope",
                        root_id=f"drop-root-{suffix}",
                        actor_id="owner",
                        justification="overflow fixture",
                        effective_from=occurred_at,
                    )
                    for suffix in ("a", "b")
                ]
            )
            await db.commit()

        errors: list[ResourceGateError] = []
        async with metadata_session_factory() as db:
            adapter = CommunitySqlAlchemyResourceGateAdapter(
                db,
                metadata_collection_limit=1,
            )
            cases = (
                ("spec", "overflow-architecture", "architecture"),
                (
                    "spec",
                    "overflow-finding-run",
                    "architecture_finding_run",
                ),
                ("spec", "overflow-mockup", "mockup"),
                ("card", "overflow-card-json", "knowledge_base"),
                ("spec", "overflow-kb-row", "knowledge_base"),
                ("card", "overflow-assignment", "knowledge_assignment"),
                ("card", "overflow-tombstone", "knowledge_tombstone"),
            )
            statements: list[str] = []
            sync_engine = db.get_bind().engine

            def capture(
                _conn: object,
                _cursor: object,
                statement: str,
                _parameters: object,
                _context: object,
                _executemany: bool,
            ) -> None:
                statements.append(statement)

            event.listen(sync_engine, "before_cursor_execute", capture)
            try:
                for entity_type, entity_id, resource_type in cases:
                    ref = await adapter.load_entity_ref_metadata(
                        "overflow-board",
                        entity_type,
                        entity_id,
                    )
                    with pytest.raises(ResourceGateError) as raised:
                        await adapter.collect_refs_metadata(ref)
                    error = raised.value
                    assert error.code == "resource_gate_metadata_collection_overflow"
                    assert error.details == {
                        "resource_type": resource_type,
                        "limit": 1,
                        "owner_ref": f"{entity_type}:{entity_id}",
                        "observed_at_least": 2,
                    }
                    assert "limit" in statements[-1].lower()
                    errors.append(error)
            finally:
                event.remove(sync_engine, "before_cursor_execute", capture)
        return errors

    errors = asyncio.run(drive())
    assert len(errors) == 7
