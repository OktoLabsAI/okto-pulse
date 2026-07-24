"""Dedicated seam evidence for Spec B scenario ``ts_2215bcca``.

The browser Playwright harness in this repository targets an already-running
installation and does not own an isolated backend/database lifecycle. Running
it here would either mock the REST boundary or mutate the user's live Pulse
data. This test therefore exercises the strongest hermetic seam available:

* the shipped selector contract is audited for authoritative omitted and
  explicit-empty DROP payloads;
* the real Community REST handlers execute the real Core use cases;
* the real SQLAlchemy propagation and Resource Gate adapters share one
  temporary SQLite database; and
* the post-DROP state proves Resource Gate N/A isolation and append-history
  preservation.

Interactive rendering remains covered by the focused Vitest selector and
CardKnowledgeTab suites.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from okto_pulse.community.adapters.sqlalchemy_base import Base
from okto_pulse.community.adapters.sqlalchemy_database import (
    install_community_sqlite_pragmas,
)
from okto_pulse.community.adapters.sqlalchemy_knowledge_propagation import (
    CommunitySqlAlchemyKnowledgePropagationStore,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Board,
    Card,
    Ideation,
    KnowledgeAssignmentRecord,
    KnowledgePropagationScopeRecord,
    Refinement,
    ResourceNotApplicable,
    Spec,
    SpecKnowledgeBase,
)
from okto_pulse.community.adapters.sqlalchemy_resource_gate_service import (
    CommunitySqlAlchemyResourceGateAdapter,
)
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import (
    CommunityUnitOfWorkFactory,
)
from okto_pulse.community.api import cards as cards_api
from okto_pulse.core.domain.enums import (
    IdeationStatus,
    RefinementStatus,
    SpecStatus,
)
from okto_pulse.core.domain.knowledge_selection import (
    KnowledgeOriginClass,
    KnowledgePropagationMode,
    KnowledgeSelectionState,
    KnowledgeTargetType,
)
from okto_pulse.core.models.knowledge_propagation import (
    KnowledgeAssignmentDropRequest,
    KnowledgeAssignmentReplaceRequest,
)
from okto_pulse.core.ports.knowledge_propagation import (
    KnowledgeScopeLookup,
    KnowledgeTargetKey,
    register_knowledge_propagation_port,
)
from okto_pulse.core.ports.relational_services import (
    register_resource_gate_adapter_factory,
)
from okto_pulse.core.services.knowledge_propagation import (
    KnowledgePropagationService,
)
from okto_pulse.core.services.resource_gate import ResourceGateService


REPO_ROOT = Path(__file__).resolve().parents[1]
BOARD_ID = "board-ts-2215bcca"
ACTOR_ID = "actor-ts-2215bcca"
SPEC_ID = "spec-ts-2215bcca"
CARD_ID = "card-ts-2215bcca"
ROOT_ID = "root-ts-2215bcca"


def _assert_shipped_selector_contract() -> None:
    choice = (
        REPO_ROOT
        / "frontend"
        / "src"
        / "components"
        / "shared"
        / "knowledgePropagationChoice.ts"
    ).read_text(encoding="utf-8")
    selector = (
        REPO_ROOT
        / "frontend"
        / "src"
        / "components"
        / "shared"
        / "KnowledgePropagationSelector.tsx"
    ).read_text(encoding="utf-8")
    card_tab = (
        REPO_ROOT
        / "frontend"
        / "src"
        / "components"
        / "kanban"
        / "CardKnowledgeTab.tsx"
    ).read_text(encoding="utf-8")

    assert "action: 'omitted'" in choice
    assert "? 'explicit_empty'" in choice
    assert "choice.action === 'drop' && knowledgeIds.length === 0" in choice
    assert "mode: choice.action" in choice
    assert "knowledge_ids: knowledgeIds" in choice
    assert "No resource starts selected" in selector
    assert "dropCardKnowledgeAssignments" in card_tab
    assert "DROP and explicit empty do not mark Resource Gate as N/A." in card_tab
    assert "markResourceNotApplicable" not in card_tab


@pytest.fixture
async def b7_runtime(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'ts-2215bcca.db'}"
    )
    install_community_sqlite_pragmas(engine)
    sessions = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    try:
        async with sessions() as session:
            session.add(
                Board(
                    id=BOARD_ID,
                    name="B7 selector evidence",
                    owner_id=ACTOR_ID,
                    realm_id="local",
                )
            )
            await session.flush()
            session.add(
                Ideation(
                    id="ideation-ts-2215bcca",
                    board_id=BOARD_ID,
                    title="B7 source ideation",
                    status=IdeationStatus.DONE,
                    created_by=ACTOR_ID,
                )
            )
            await session.flush()
            session.add(
                Refinement(
                    id="refinement-ts-2215bcca",
                    ideation_id="ideation-ts-2215bcca",
                    board_id=BOARD_ID,
                    title="B7 source refinement",
                    status=RefinementStatus.DONE,
                    created_by=ACTOR_ID,
                )
            )
            await session.flush()
            session.add(
                Spec(
                    id=SPEC_ID,
                    board_id=BOARD_ID,
                    refinement_id="refinement-ts-2215bcca",
                    title="B7 source spec",
                    status=SpecStatus.APPROVED,
                    created_by=ACTOR_ID,
                )
            )
            await session.flush()
            session.add_all(
                [
                    SpecKnowledgeBase(
                        id="kb-source-ts-2215bcca",
                        spec_id=SPEC_ID,
                        title="Canonical selectable source",
                        content="source revision one",
                        source_version=1,
                        source_kb_id=ROOT_ID,
                        immediate_parent_kb_id=ROOT_ID,
                        root_source_kb_id=ROOT_ID,
                        created_by=ACTOR_ID,
                    ),
                    Card(
                        id=CARD_ID,
                        board_id=BOARD_ID,
                        spec_id=SPEC_ID,
                        title="B7 target card",
                        created_by=ACTOR_ID,
                        knowledge_bases=[
                            {
                                "id": "legacy-card-kb",
                                "title": "Legacy physical history",
                                "description": None,
                                "content": "must survive v2 explicit empty",
                                "mime_type": "text/markdown",
                            }
                        ],
                    ),
                    ResourceNotApplicable(
                        id="na-architecture-ts-2215bcca",
                        board_id=BOARD_ID,
                        entity_type="card",
                        entity_id=CARD_ID,
                        resource_type="architecture",
                        justification="Pre-existing unrelated gate decision.",
                        source_channel="ui",
                        active=True,
                        created_by=ACTOR_ID,
                    ),
                ]
            )
            await session.commit()

        store = CommunitySqlAlchemyKnowledgePropagationStore(sessions)
        register_knowledge_propagation_port(store)
        register_resource_gate_adapter_factory(
            CommunitySqlAlchemyResourceGateAdapter
        )
        yield store, sessions, CommunityUnitOfWorkFactory(sessions)
    finally:
        await engine.dispose()


def _gate_mark_projection(rows: list[ResourceNotApplicable]) -> list[tuple[object, ...]]:
    return [
        (
            row.id,
            row.entity_type,
            row.entity_id,
            row.resource_type,
            row.justification,
            row.source_channel,
            row.active,
            row.cleared_by,
            row.clear_reason,
        )
        for row in rows
    ]


async def test_ts_2215bcca_b7_selector_drop_keeps_gate_and_history(
    b7_runtime,
) -> None:
    """AC-B15/B17: explicit empty is persisted without N/A or history drift."""

    _assert_shipped_selector_contract()
    store, sessions, uow_factory = b7_runtime
    target = KnowledgeTargetKey(
        board_id=BOARD_ID,
        target_type=KnowledgeTargetType.CARD,
        target_id=CARD_ID,
    )

    async with sessions() as session:
        before_rows = list(
            (
                await session.execute(
                    select(ResourceNotApplicable).order_by(ResourceNotApplicable.id)
                )
            )
            .scalars()
            .all()
        )
        before_marks = _gate_mark_projection(before_rows)
        before_summary = await ResourceGateService(session).get_summary(
            BOARD_ID,
            "card",
            CARD_ID,
        )

    async with uow_factory() as uow:
        reference = await cards_api.replace_card_knowledge_assignments(
            CARD_ID,
            KnowledgeAssignmentReplaceRequest(
                contract_version=2,
                mode="reference",
                knowledge_ids=["kb-source-ts-2215bcca"],
                justification="Reference is relevant before the explicit drop.",
                idempotency_key="ts-2215bcca-reference",
                expected_revision=0,
            ),
            user_id=ACTOR_ID,
            uow=uow,
        )
    assert reference.selection_state is KnowledgeSelectionState.EXPLICIT_IDS
    assert reference.revision == 1
    assert [item.mode for item in reference.assignments] == [
        KnowledgePropagationMode.REFERENCE
    ]
    assert [item.root_knowledge_id for item in reference.assignments] == [ROOT_ID]

    async with uow_factory() as uow:
        dropped = await cards_api.drop_card_knowledge_assignments(
            CARD_ID,
            KnowledgeAssignmentDropRequest(
                contract_version=2,
                knowledge_ids=[],
                justification="No inherited Knowledge applies to this target.",
                idempotency_key="ts-2215bcca-explicit-empty",
                expected_revision=1,
            ),
            user_id=ACTOR_ID,
            uow=uow,
        )
    assert dropped.selection_state is KnowledgeSelectionState.EXPLICIT_EMPTY
    assert dropped.revision == 2
    assert dropped.assignments == []

    async with uow_factory() as uow:
        technical = await cards_api.get_card_knowledge_assignments(
            CARD_ID,
            user_id=ACTOR_ID,
            uow=uow,
        )
    assert technical.selection_state is KnowledgeSelectionState.EXPLICIT_EMPTY
    assert technical.revision == 2
    assert technical.assignments == []

    async with sessions() as session:
        scope = await store.load_scope(
            session,
            KnowledgeScopeLookup(target=target),
        )
        read = await KnowledgePropagationService(port=store).read(session, target)
        assignment_rows = list(
            (
                await session.execute(
                    select(KnowledgeAssignmentRecord).order_by(
                        KnowledgeAssignmentRecord.assignment_id
                    )
                )
            )
            .scalars()
            .all()
        )
        scope_row = (
            await session.execute(
                select(KnowledgePropagationScopeRecord).where(
                    KnowledgePropagationScopeRecord.board_id == BOARD_ID,
                    KnowledgePropagationScopeRecord.target_type == "card",
                    KnowledgePropagationScopeRecord.target_id == CARD_ID,
                )
            )
        ).scalar_one()
        card = await session.get(Card, CARD_ID)
        after_rows = list(
            (
                await session.execute(
                    select(ResourceNotApplicable).order_by(ResourceNotApplicable.id)
                )
            )
            .scalars()
            .all()
        )
        after_summary = await ResourceGateService(session).get_summary(
            BOARD_ID,
            "card",
            CARD_ID,
        )

    assert scope.scope_revision == 2
    assert scope.selection_state is KnowledgeSelectionState.EXPLICIT_EMPTY
    assert scope_row.selection_state == "explicit_empty"
    assert not any(item.temporal.is_current for item in scope.assignments)
    assert len(assignment_rows) == 1
    historical_assignment = assignment_rows[0]
    assert historical_assignment.mode == "reference"
    assert historical_assignment.state == "active"
    assert historical_assignment.origin_class == "v2"
    assert historical_assignment.root_id == ROOT_ID
    assert historical_assignment.effective_to is not None

    assert len(read.history_assignments) == 1
    assert (
        read.history_assignments[0].assignment.origin_class
        is KnowledgeOriginClass.V2
    )
    assert read.effective_assignments == ()
    assert read.effective_legacy_attachments == ()
    assert any(
        item.source_knowledge_id == "legacy-card-kb"
        and item.origin_class is KnowledgeOriginClass.LEGACY_ALL
        for item in read.history_legacy_attachments
    )
    assert card is not None
    assert card.knowledge_bases == [
        {
            "id": "legacy-card-kb",
            "title": "Legacy physical history",
            "description": None,
            "content": "must survive v2 explicit empty",
            "mime_type": "text/markdown",
        }
    ]

    assert _gate_mark_projection(after_rows) == before_marks
    assert not any(row.resource_type == "knowledge_base" for row in after_rows)
    before_knowledge = next(
        item
        for item in before_summary["resources"]
        if item["resource_type"] == "knowledge_base"
    )
    after_knowledge = next(
        item
        for item in after_summary["resources"]
        if item["resource_type"] == "knowledge_base"
    )
    assert before_knowledge["na_mark"] is None
    assert after_knowledge["na_mark"] is None
