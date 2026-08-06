"""Scenario-level integration evidence for selective Knowledge propagation Spec B.

These tests deliberately cross the public REST/MCP adapters, Core use cases, the
Community UoW, and the real SQLAlchemy persistence adapter.  Lower-level suites
remain responsible for exhaustive fault injection; this file supplies the
stable, replayable pointers required by Pulse scenarios B1-B3.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from okto_pulse.community.adapters.knowledge_propagation_backfill import (
    backfill_knowledge_propagation_v2,
)
from okto_pulse.community.adapters.relational_application import (
    CommunityRelationalApplicationAdapter,
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
from okto_pulse.community.adapters.sqlalchemy_domain_event_delivery import (
    CommunitySqlAlchemyDomainEventPublisher,
)
from okto_pulse.community.adapters.sqlalchemy_knowledge_propagation import (
    CommunitySqlAlchemyKnowledgePropagationStore,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Board,
    Card,
    Ideation,
    KnowledgeAssignmentRecord,
    KnowledgeMutationLedgerRecord,
    KnowledgePropagationScopeRecord,
    KnowledgeSnapshotRecord,
    KnowledgeTombstoneRecord,
    Refinement,
    RefinementKnowledgeBase,
    Spec,
    SpecKnowledgeBase,
)
from okto_pulse.community.adapters.sqlalchemy_spec_resource_propagation import (
    CommunitySqlAlchemySpecResourcePropagationStore,
)
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import (
    CommunityUnitOfWorkFactory,
)
from okto_pulse.community.api import cards as cards_api
from okto_pulse.community.api import refinements as refinements_api
from okto_pulse.core.application.use_cases import ActorContext
from okto_pulse.core.domain.enums import (
    IdeationStatus,
    RefinementStatus,
    SpecStatus,
)
from okto_pulse.core.domain.knowledge_selection import (
    KnowledgeAssignmentState,
    KnowledgePropagationMode,
    KnowledgeSelectionState,
    KnowledgeTargetType,
)
from okto_pulse.core.mcp import server as mcp_server
from okto_pulse.core.models.knowledge_propagation import (
    DeriveSpecKnowledgeRequest,
    KnowledgeAssignmentDropRequest,
    KnowledgeAssignmentRefreshRequest,
    KnowledgeAssignmentReplaceRequest,
    KnowledgePropagationEnvelopeV2,
)
from okto_pulse.core.ports.knowledge_propagation import (
    KnowledgeParentKey,
    KnowledgeParentType,
    KnowledgeTargetKey,
    register_knowledge_mutation_audit_sink,
    register_knowledge_propagation_port,
)
from okto_pulse.core.ports.relational_application import (
    register_relational_application_adapter,
    reset_relational_application_adapter_for_tests,
)
from okto_pulse.core.ports.architecture_persistence import (
    register_architecture_persistence_port,
)
from okto_pulse.core.ports.domain_event_delivery import (
    register_domain_event_publisher,
)
from okto_pulse.core.ports.spec_resource_propagation import (
    register_spec_resource_propagation_store,
)
from okto_pulse.core.runtime_registry import register_unit_of_work_factory
from okto_pulse.core.services.knowledge_propagation import (
    KnowledgePropagationService,
    KnowledgeRelinkResetCommand,
)
from okto_pulse.core.services.spec_resource_propagation import (
    SpecResourcePropagationService,
)


BOARD_ID = "spec-b-e2e-board"
ACTOR_ID = "spec-b-e2e-agent"
NOW = datetime(2026, 7, 23, 18, 0, tzinfo=timezone.utc)

# The production REST path always creates the unit of work with the
# authenticated principal bound (R01B FR3). Creating actor-less UoWs here only
# passed while the process-wide semantic subject versioning listeners were not
# installed; any earlier test that builds the real session factory installs
# them and the semantic bridge then (correctly) fails closed at commit with
# semantic_subject_mutation_actor_required. Bind the same actor the endpoints
# receive as ``user_id`` so the test matches the production wiring in both
# worlds.
REST_ACTOR = ActorContext(
    ACTOR_ID,
    "rest",
    actor_kind="human",
    board_id=BOARD_ID,
)


@dataclass(frozen=True)
class _Runtime:
    sessions: Any
    store: CommunitySqlAlchemyKnowledgePropagationStore
    uow_factory: CommunityUnitOfWorkFactory


@pytest.fixture
async def spec_b_runtime(tmp_path) -> _Runtime:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'spec-b-knowledge-e2e.db'}"
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

    store = CommunitySqlAlchemyKnowledgePropagationStore(sessions)
    uow_factory = CommunityUnitOfWorkFactory(sessions)
    register_knowledge_propagation_port(store)
    register_knowledge_mutation_audit_sink(store)
    register_unit_of_work_factory(uow_factory)
    register_relational_application_adapter(
        CommunityRelationalApplicationAdapter()
    )
    register_architecture_persistence_port(CommunitySqlAlchemyArchitecturePersistence())
    register_domain_event_publisher(CommunitySqlAlchemyDomainEventPublisher())
    register_spec_resource_propagation_store(
        CommunitySqlAlchemySpecResourcePropagationStore()
    )
    try:
        yield _Runtime(sessions=sessions, store=store, uow_factory=uow_factory)
    finally:
        reset_relational_application_adapter_for_tests()
        await engine.dispose()


def _install_real_mcp_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _agent_context(board_id: str) -> Any:
        return SimpleNamespace(
            agent_id=ACTOR_ID,
            agent_name="Spec B E2E",
            permissions=None,
            realm_id=None,
            board_id=board_id,
        )

    monkeypatch.setattr(mcp_server, "_get_agent_ctx", _agent_context)


async def _seed_refinement_sources(
    runtime: _Runtime,
    *,
    refinement_id: str,
    roots: tuple[str, ...],
) -> None:
    ideation_id = f"{refinement_id}-ideation"
    async with runtime.sessions() as session:
        session.add(
            Board(
                id=BOARD_ID,
                name="Spec B selective propagation",
                owner_id=ACTOR_ID,
                realm_id="local",
            )
        )
        session.add(
            Ideation(
                id=ideation_id,
                board_id=BOARD_ID,
                title="Spec B source ideation",
                status=IdeationStatus.DONE,
                created_by=ACTOR_ID,
            )
        )
        await session.flush()
        session.add(
            Refinement(
                id=refinement_id,
                ideation_id=ideation_id,
                board_id=BOARD_ID,
                title="Spec B source refinement",
                status=RefinementStatus.DONE,
                created_by=ACTOR_ID,
            )
        )
        await session.flush()
        session.add_all(
            [
                RefinementKnowledgeBase(
                    id=f"{root}-v1",
                    refinement_id=refinement_id,
                    title=f"Knowledge {root}",
                    content=f"{root}:revision-one",
                    source_version=1,
                    root_source_kb_id=root,
                    created_by=ACTOR_ID,
                )
                for root in roots
            ]
        )
        await session.commit()


async def _seed_spec_cards(
    runtime: _Runtime,
    *,
    spec_id: str,
    card_ids: tuple[str, ...],
    roots: tuple[str, ...],
) -> None:
    async with runtime.sessions() as session:
        session.add(
            Board(
                id=BOARD_ID,
                name="Spec B selective propagation",
                owner_id=ACTOR_ID,
                realm_id="local",
                settings={
                    "auto_derive_spec_resources_enabled": True,
                    "auto_derive_spec_resource_types": ["knowledge_base"],
                },
            )
        )
        session.add(
            Spec(
                id=spec_id,
                board_id=BOARD_ID,
                title="Spec B source spec",
                status=SpecStatus.APPROVED,
                created_by=ACTOR_ID,
            )
        )
        await session.flush()
        session.add_all(
            [
                SpecKnowledgeBase(
                    id=f"{root}-v1",
                    spec_id=spec_id,
                    title=f"Knowledge {root}",
                    content=f"{root}:revision-one",
                    source_version=1,
                    root_source_kb_id=root,
                    created_by=ACTOR_ID,
                )
                for root in roots
            ]
        )
        session.add_all(
            [
                Card(
                    id=card_id,
                    board_id=BOARD_ID,
                    spec_id=spec_id,
                    title=f"Target {card_id}",
                    created_by=ACTOR_ID,
                    knowledge_bases=[],
                )
                for card_id in card_ids
            ]
        )
        await session.commit()


async def _scope_rows(runtime: _Runtime, target_id: str) -> dict[str, Any]:
    async with runtime.sessions() as session:
        scope = (
            await session.execute(
                select(KnowledgePropagationScopeRecord).where(
                    KnowledgePropagationScopeRecord.target_id == target_id
                )
            )
        ).scalar_one()
        assignments = (
            (
                await session.execute(
                    select(KnowledgeAssignmentRecord)
                    .where(KnowledgeAssignmentRecord.scope_id == scope.id)
                    .order_by(KnowledgeAssignmentRecord.assignment_id)
                )
            )
            .scalars()
            .all()
        )
        snapshots = (
            (
                await session.execute(
                    select(KnowledgeSnapshotRecord)
                    .where(KnowledgeSnapshotRecord.scope_id == scope.id)
                    .order_by(KnowledgeSnapshotRecord.snapshot_id)
                )
            )
            .scalars()
            .all()
        )
        tombstones = (
            (
                await session.execute(
                    select(KnowledgeTombstoneRecord)
                    .where(KnowledgeTombstoneRecord.scope_id == scope.id)
                    .order_by(KnowledgeTombstoneRecord.tombstone_id)
                )
            )
            .scalars()
            .all()
        )
        ledgers = (
            (
                await session.execute(
                    select(KnowledgeMutationLedgerRecord)
                    .where(KnowledgeMutationLedgerRecord.scope_id == scope.id)
                    .order_by(KnowledgeMutationLedgerRecord.revision)
                )
            )
            .scalars()
            .all()
        )
    return {
        "scope": scope,
        "assignments": assignments,
        "snapshots": snapshots,
        "tombstones": tombstones,
        "ledgers": ledgers,
    }


@pytest.mark.asyncio
async def test_ts_9e54d02f_tri_state_v2_end_to_end(
    spec_b_runtime: _Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B1: explicit v2 tri-state survives MCP/REST/Core/SQL without fallback."""

    runtime = spec_b_runtime
    refinement_id = "b1-refinement"
    roots = ("b1-root-a", "b1-root-b", "b1-root-c")
    await _seed_refinement_sources(
        runtime,
        refinement_id=refinement_id,
        roots=roots,
    )
    _install_real_mcp_runtime(monkeypatch)
    monkeypatch.setattr(
        refinements_api,
        "get_unit_of_work_factory",
        lambda _request: runtime.uow_factory,
    )

    omitted_payload = json.loads(
        await mcp_server.okto_pulse_derive_spec_from_refinement.fn(
            board_id=BOARD_ID,
            refinement_id=refinement_id,
            knowledge_propagation=KnowledgePropagationEnvelopeV2(
                selection_state="omitted",
                idempotency_key="b1-omitted",
            ),
        )
    )
    assert omitted_payload["success"] is True
    assert omitted_payload["selection_state"] == "omitted"

    request = SimpleNamespace()
    async with runtime.uow_factory(actor=REST_ACTOR) as rest_uow:
        explicit_empty = await refinements_api.derive_spec(
            refinement_id,
            request=request,  # type: ignore[arg-type]
            data=DeriveSpecKnowledgeRequest(
                knowledge_propagation=KnowledgePropagationEnvelopeV2(
                    selection_state="explicit_empty",
                    mode="drop",
                    justification="No inherited Knowledge is relevant",
                    idempotency_key="b1-explicit-empty",
                )
            ),
            user_id=ACTOR_ID,
            uow=rest_uow,
        )
    assert explicit_empty.selection_state is KnowledgeSelectionState.EXPLICIT_EMPTY

    async with runtime.uow_factory(actor=REST_ACTOR) as rest_uow:
        explicit_ids = await refinements_api.derive_spec(
            refinement_id,
            request=request,  # type: ignore[arg-type]
            data=DeriveSpecKnowledgeRequest(
                knowledge_propagation=KnowledgePropagationEnvelopeV2(
                    selection_state="explicit_ids",
                    mode="reference",
                    knowledge_ids=[
                        f"{roots[1]}-v1",
                        f"{roots[0]}-v1",
                    ],
                    justification="Only the first two roots are relevant",
                    idempotency_key="b1-explicit-ids",
                )
            ),
            user_id=ACTOR_ID,
            uow=rest_uow,
        )
    assert explicit_ids.selection_state is KnowledgeSelectionState.EXPLICIT_IDS
    assert {item.root_knowledge_id for item in explicit_ids.assignments} == set(
        roots[:2]
    )

    omitted = await _scope_rows(runtime, omitted_payload["spec_id"])
    empty = await _scope_rows(runtime, explicit_empty.spec_id)
    selected = await _scope_rows(runtime, explicit_ids.spec_id)

    assert omitted["scope"].selection_state == "omitted"
    assert omitted["scope"].v2_active is True
    assert omitted["assignments"] == []
    assert omitted["snapshots"] == []
    assert omitted["tombstones"] == []
    assert len(omitted["ledgers"]) == 1

    assert empty["scope"].selection_state == "explicit_empty"
    assert empty["assignments"] == []
    assert empty["snapshots"] == []
    assert [item.root_id for item in empty["tombstones"]] == [None]
    assert len(empty["ledgers"]) == 1

    current_selected = [
        item for item in selected["assignments"] if item.effective_to is None
    ]
    assert selected["scope"].selection_state == "explicit_ids"
    assert {item.root_id for item in current_selected} == set(roots[:2])
    assert selected["snapshots"] == []
    assert selected["tombstones"] == []
    assert len(selected["ledgers"]) == 1
    assert roots[2] not in {item.root_id for item in selected["assignments"]}


def _card_target(card_id: str) -> KnowledgeTargetKey:
    return KnowledgeTargetKey(
        board_id=BOARD_ID,
        target_type=KnowledgeTargetType.CARD,
        target_id=card_id,
    )


async def _read_card(runtime: _Runtime, card_id: str) -> Any:
    async with runtime.sessions() as session:
        return await KnowledgePropagationService(port=runtime.store).read(
            session,
            _card_target(card_id),
        )


def _resolved_by_root(read_result: Any) -> dict[str, Any]:
    return {
        item.assignment.revision_stamp.root_id: item
        for item in read_result.resolved_assignments
    }


@pytest.mark.asyncio
async def test_ts_1e0f5761_reference_snapshot_temporal_semantics(
    spec_b_runtime: _Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B2: two references track current sources while two snapshots stay frozen."""

    runtime = spec_b_runtime
    spec_id = "b2-spec"
    reference_card = "b2-reference-card"
    snapshot_card = "b2-snapshot-card"
    reference_roots = ("b2-ref-a", "b2-ref-b")
    snapshot_roots = ("b2-snap-a", "b2-snap-b")
    all_roots = reference_roots + snapshot_roots
    await _seed_spec_cards(
        runtime,
        spec_id=spec_id,
        card_ids=(reference_card, snapshot_card),
        roots=all_roots,
    )
    _install_real_mcp_runtime(monkeypatch)

    reference_payload = json.loads(
        await mcp_server.okto_pulse_replace_card_knowledge_assignments.fn(
            board_id=BOARD_ID,
            card_id=reference_card,
            request=KnowledgeAssignmentReplaceRequest(
                knowledge_ids=[f"{root}-v1" for root in reference_roots],
                mode="reference",
                justification="Follow both current source revisions",
                idempotency_key="b2-reference-select",
                expected_revision=0,
            ),
        )
    )
    assert reference_payload["success"] is True
    assert {
        item["root_knowledge_id"] for item in reference_payload["assignments"]
    } == set(reference_roots)

    async with runtime.uow_factory(actor=REST_ACTOR) as rest_uow:
        snapshot_response = await cards_api.replace_card_knowledge_assignments(
            snapshot_card,
            KnowledgeAssignmentReplaceRequest(
                knowledge_ids=[f"{root}-v1" for root in snapshot_roots],
                mode="snapshot",
                justification="Freeze both source revisions",
                idempotency_key="b2-snapshot-select",
                expected_revision=0,
            ),
            user_id=ACTOR_ID,
            uow=rest_uow,
        )
    assert {item.root_knowledge_id for item in snapshot_response.assignments} == set(
        snapshot_roots
    )

    reference_before = _resolved_by_root(await _read_card(runtime, reference_card))
    snapshot_before = _resolved_by_root(await _read_card(runtime, snapshot_card))
    assert set(reference_before) == set(reference_roots)
    assert set(snapshot_before) == set(snapshot_roots)
    assert all(
        item.state is KnowledgeAssignmentState.ACTIVE
        and item.assignment.mode is KnowledgePropagationMode.REFERENCE
        for item in reference_before.values()
    )
    assert all(
        item.state is KnowledgeAssignmentState.ACTIVE
        and item.assignment.mode is KnowledgePropagationMode.SNAPSHOT
        for item in snapshot_before.values()
    )
    frozen_bytes = {root: item.content_bytes for root, item in snapshot_before.items()}
    frozen_stamps = {
        root: item.revision_stamp for root, item in snapshot_before.items()
    }
    reference_bytes = {
        root: item.content_bytes for root, item in reference_before.items()
    }

    reference_rows = await _scope_rows(runtime, reference_card)
    snapshot_rows = await _scope_rows(runtime, snapshot_card)
    assert reference_rows["snapshots"] == []
    assert (
        len(
            [
                item
                for item in reference_rows["assignments"]
                if item.effective_to is None
            ]
        )
        == 2
    )
    assert (
        len(
            [item for item in snapshot_rows["assignments"] if item.effective_to is None]
        )
        == 2
    )
    assert (
        len([item for item in snapshot_rows["snapshots"] if item.effective_to is None])
        == 2
    )

    async with runtime.sessions() as session:
        for root in all_roots:
            source = await session.get(SpecKnowledgeBase, f"{root}-v1")
            assert source is not None
            source.content = f"{root}:revision-two"
            source.source_version = 2
        await session.commit()

    reference_changed = _resolved_by_root(await _read_card(runtime, reference_card))
    snapshot_stale = _resolved_by_root(await _read_card(runtime, snapshot_card))
    assert all(
        item.state is KnowledgeAssignmentState.ACTIVE
        and item.content_bytes != reference_bytes[root]
        and item.revision_stamp.source_revision == "2"
        for root, item in reference_changed.items()
    )
    assert all(
        item.state is KnowledgeAssignmentState.STALE
        and item.content_bytes == frozen_bytes[root]
        and item.revision_stamp == frozen_stamps[root]
        for root, item in snapshot_stale.items()
    )

    refresh_payload = json.loads(
        await mcp_server.okto_pulse_refresh_card_knowledge_assignments.fn(
            board_id=BOARD_ID,
            card_id=snapshot_card,
            request=KnowledgeAssignmentRefreshRequest(
                knowledge_ids=list(snapshot_roots),
                idempotency_key="b2-snapshot-refresh",
                expected_revision=1,
            ),
        )
    )
    assert refresh_payload["success"] is True
    assert {item["root_knowledge_id"] for item in refresh_payload["refreshed"]} == set(
        snapshot_roots
    )
    assert all(
        item["source_revision"] == "2" and len(item["source_content_sha256"]) == 64
        for item in refresh_payload["refreshed"]
    )

    snapshot_refreshed = _resolved_by_root(await _read_card(runtime, snapshot_card))
    assert all(
        item.state is KnowledgeAssignmentState.ACTIVE
        and item.content_bytes != frozen_bytes[root]
        and item.revision_stamp.source_revision == "2"
        for root, item in snapshot_refreshed.items()
    )
    refreshed_rows = await _scope_rows(runtime, snapshot_card)
    assert refreshed_rows["scope"].scope_revision == 2
    assert len(refreshed_rows["assignments"]) == 4
    assert len(refreshed_rows["snapshots"]) == 4
    assert (
        len(
            [
                item
                for item in refreshed_rows["assignments"]
                if item.effective_to is None
            ]
        )
        == 2
    )
    assert (
        len([item for item in refreshed_rows["snapshots"] if item.effective_to is None])
        == 2
    )
    assert [item.operation_kind for item in refreshed_rows["ledgers"]] == [
        "replace",
        "refresh_snapshot",
    ]


@pytest.mark.asyncio
async def test_ts_f9c3c8e0_drop_survives_reconcilers_and_source_delete(
    spec_b_runtime: _Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B3: a root DROP survives copy/backfill/restart and source deletion."""

    runtime = spec_b_runtime
    spec_id = "b3-spec"
    relink_spec_id = "b3-relink-spec"
    card_id = "b3-card"
    dropped_root = "b3-root-dropped"
    active_root = "b3-root-active"
    roots = (dropped_root, active_root)
    await _seed_spec_cards(
        runtime,
        spec_id=spec_id,
        card_ids=(card_id,),
        roots=roots,
    )
    async with runtime.sessions() as session:
        session.add(
            Spec(
                id=relink_spec_id,
                board_id=BOARD_ID,
                title="Spec B relink destination",
                status=SpecStatus.APPROVED,
                created_by=ACTOR_ID,
            )
        )
        await session.flush()
        session.add_all(
            [
                SpecKnowledgeBase(
                    id=f"{root}-relink-v1",
                    spec_id=relink_spec_id,
                    title=f"Relink Knowledge {root}",
                    content=f"{root}:relink-revision-one",
                    source_version=1,
                    root_source_kb_id=root,
                    created_by=ACTOR_ID,
                )
                for root in roots
            ]
        )
        await session.commit()
    _install_real_mcp_runtime(monkeypatch)

    selected = json.loads(
        await mcp_server.okto_pulse_replace_card_knowledge_assignments.fn(
            board_id=BOARD_ID,
            card_id=card_id,
            request=KnowledgeAssignmentReplaceRequest(
                knowledge_ids=[f"{root}-v1" for root in roots],
                mode="reference",
                justification="Both roots are initially relevant",
                idempotency_key="b3-select-both",
                expected_revision=0,
            ),
        )
    )
    assert selected["success"] is True

    async with runtime.uow_factory(actor=REST_ACTOR) as rest_uow:
        dropped = await cards_api.drop_card_knowledge_assignments(
            card_id,
            KnowledgeAssignmentDropRequest(
                knowledge_ids=[dropped_root],
                justification="This root is explicitly out of scope",
                idempotency_key="b3-drop-one",
                expected_revision=1,
            ),
            user_id=ACTOR_ID,
            uow=rest_uow,
        )
    assert dropped.selection_state is KnowledgeSelectionState.EXPLICIT_IDS
    assert {item.root_knowledge_id: item.state for item in dropped.assignments}[
        dropped_root
    ] is KnowledgeAssignmentState.DROPPED

    initial_read = _resolved_by_root(await _read_card(runtime, card_id))
    assert initial_read[dropped_root].state is KnowledgeAssignmentState.DROPPED
    assert initial_read[dropped_root].effective is False
    assert initial_read[active_root].state is KnowledgeAssignmentState.ACTIVE
    assert initial_read[active_root].effective is True
    active_before_update = initial_read[active_root].content_bytes

    initial_rows = await _scope_rows(runtime, card_id)
    assert initial_rows["scope"].scope_revision == 2
    assert [
        item.root_id for item in initial_rows["tombstones"] if item.effective_to is None
    ] == [dropped_root]

    # Exercise the real resource reconciler through the trigger names used by
    # create, relink/update, source update, and settings backfill paths.
    async with runtime.sessions() as session:
        propagation = SpecResourcePropagationService(
            session,
            knowledge_propagation_port=runtime.store,
        )
        for trigger in (
            "card_created",
            "card_linked_via_update",
            "spec_knowledge_updated",
        ):
            result = await propagation.propagate_for_card(
                board_id=BOARD_ID,
                spec_id=spec_id,
                card_id=card_id,
                actor_id=ACTOR_ID,
                trigger=trigger,
            )
            assert result["results"]["knowledge_base"] == {
                "source_count": 0,
                "copied_count": 0,
                "ignored_count": 0,
                "copied_ids": [],
                "removed_count": 0,
                "removed_ids": [],
                "warnings": [],
                "skipped": True,
                "reason": "v2_active",
            }
        board_backfill = await propagation.propagate_for_board(
            board_id=BOARD_ID,
            actor_id=ACTOR_ID,
            trigger="board_settings_backfill",
        )
        card_results = [
            card_result
            for spec_result in board_backfill["specs"]
            for card_result in spec_result["cards"]
            if card_result.get("card_id") == card_id
        ]
        assert len(card_results) == 1
        assert card_results[0]["results"]["knowledge_base"]["reason"] == "v2_active"
        await session.commit()

    legacy_copy = json.loads(
        await mcp_server.okto_pulse_copy_knowledge_to_card.fn(
            board_id=BOARD_ID,
            spec_id=spec_id,
            card_id=card_id,
            knowledge_ids=[],
        )
    )
    assert legacy_copy["code"] == "knowledge_propagation_legacy_write_forbidden"
    async with runtime.sessions() as session:
        card = await session.get(Card, card_id)
        assert card is not None
        assert card.knowledge_bases == []

    backfill = await backfill_knowledge_propagation_v2(
        session_factory=runtime.sessions,
        store=runtime.store,
        service=KnowledgePropagationService(port=runtime.store),
    )
    assert backfill.active_v2_targets >= 1
    after_backfill = await _scope_rows(runtime, card_id)
    assert after_backfill["scope"].scope_revision == 2
    assert [
        item.root_id
        for item in after_backfill["tombstones"]
        if item.effective_to is None
    ] == [dropped_root]

    async with runtime.sessions() as session:
        for root in roots:
            source = await session.get(SpecKnowledgeBase, f"{root}-v1")
            assert source is not None
            source.content = f"{root}:revision-two"
            source.source_version = 2
        await session.commit()

    restarted_store = CommunitySqlAlchemyKnowledgePropagationStore(runtime.sessions)
    async with runtime.sessions() as session:
        restarted_read = await KnowledgePropagationService(port=restarted_store).read(
            session, _card_target(card_id)
        )
    restarted = _resolved_by_root(restarted_read)
    assert restarted[dropped_root].state is KnowledgeAssignmentState.DROPPED
    assert restarted[dropped_root].effective is False
    assert restarted[active_root].state is KnowledgeAssignmentState.ACTIVE
    assert restarted[active_root].effective is True
    assert restarted[active_root].content_bytes != active_before_update

    refused_refresh = json.loads(
        await mcp_server.okto_pulse_refresh_card_knowledge_assignments.fn(
            board_id=BOARD_ID,
            card_id=card_id,
            request=KnowledgeAssignmentRefreshRequest(
                knowledge_ids=[dropped_root],
                idempotency_key="b3-refresh-dropped",
                expected_revision=2,
            ),
        )
    )
    assert refused_refresh["code"] == "knowledge_assignment_not_refreshable"
    after_refused_refresh = await _scope_rows(runtime, card_id)
    assert after_refused_refresh["scope"].scope_revision == 2

    # Deleting the remaining source removes it from effective context while
    # retaining both assignment history and the DROP tombstone.
    async with runtime.sessions() as session:
        source = await session.get(SpecKnowledgeBase, f"{active_root}-v1")
        assert source is not None
        await session.delete(source)
        await session.commit()
    async with runtime.sessions() as session:
        deleted_source_read = await KnowledgePropagationService(
            port=restarted_store
        ).read(session, _card_target(card_id))
    deleted = _resolved_by_root(deleted_source_read)
    assert deleted[dropped_root].state is KnowledgeAssignmentState.DROPPED
    assert deleted[dropped_root].effective is False
    assert deleted[active_root].state is KnowledgeAssignmentState.SOURCE_DELETED
    assert deleted[active_root].effective is False
    assert deleted_source_read.effective_count == 0
    assert {
        item.assignment.revision_stamp.root_id
        for item in deleted_source_read.history_assignments
    } == set(roots)

    # Relink reset is append-only: it closes old current records, keeps v2
    # authoritative as omitted, and therefore cannot reattach same-root rows
    # available on the destination spec.
    async with runtime.sessions() as session:
        service = KnowledgePropagationService(port=restarted_store)
        await service.reset_for_relink(
            session,
            KnowledgeRelinkResetCommand(
                target=_card_target(card_id),
                previous_parent=KnowledgeParentKey(
                    board_id=BOARD_ID,
                    parent_type=KnowledgeParentType.SPEC,
                    parent_id=spec_id,
                ),
                next_parent=KnowledgeParentKey(
                    board_id=BOARD_ID,
                    parent_type=KnowledgeParentType.SPEC,
                    parent_id=relink_spec_id,
                ),
                actor_id=ACTOR_ID,
                expected_revision=2,
                idempotency_key="b3-relink-reset",
            ),
        )
        card = await session.get(Card, card_id)
        assert card is not None
        card.spec_id = relink_spec_id
        await session.commit()
    relinked_read = await _read_card(runtime, card_id)
    assert relinked_read.selection_state is KnowledgeSelectionState.OMITTED
    assert relinked_read.effective_count == 0
    relinked_rows = await _scope_rows(runtime, card_id)
    assert relinked_rows["scope"].scope_revision == 3
    assert not any(item.effective_to is None for item in relinked_rows["assignments"])
    assert not any(item.effective_to is None for item in relinked_rows["tombstones"])
    assert len(relinked_rows["assignments"]) == 3
    assert len(relinked_rows["tombstones"]) == 1
    assert [item.operation_kind for item in relinked_rows["ledgers"]] == [
        "replace",
        "drop_delta",
        "relink_reset",
    ]
