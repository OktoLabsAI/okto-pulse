"""Authorized P1 coordinator on real disposable SQL, including outbox/history.

Transport handlers, classification UI and Spec start admission are separate
acceptance surfaces; this suite does not claim to exercise those surfaces.
"""

import asyncio
import copy

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from okto_pulse.community.adapters.sqlalchemy_domain_event_delivery import (
    CommunitySqlAlchemyDomainEventPublisher,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    ArchitectureCandidateDecisionRow,
    ArchitectureClassificationReceiptRow,
    ArchitectureDesign,
    DomainEventHandlerExecution,
    DomainEventRow,
    Spec,
    SpecHistory,
)
from okto_pulse.community.adapters.sqlalchemy_structured_spec import (
    CommunitySqlAlchemyStructuredSpecStore,
)
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import (
    CommunitySemanticSession,
)
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
from okto_pulse.core.application.use_cases.architecture_classification import (
    ClassifyArchitectureCandidatesCommand,
    ClassifyArchitectureCandidatesUseCase,
)
from okto_pulse.core.application.use_cases.base import (
    ActorContext,
    EntityNotFoundError,
    PermissionDeniedError,
)
from okto_pulse.core.domain.architecture_classification import (
    ArchitectureClassificationBatch,
    ArchitectureClassificationError,
)
from okto_pulse.core.domain.human_validation_cycle import SubjectEditRequiresDraftError
from okto_pulse.core.domain.permissions import ALL_FLAGS
from okto_pulse.core.events.handlers.consolidation_enqueuer import ConsolidationEnqueuer
from okto_pulse.core.events.registry import register_handler
from okto_pulse.core.ports.domain_event_delivery import register_domain_event_publisher
from okto_pulse.core.ports.permission_policy import PermissionSet, set_permission_flag
from okto_pulse.core.ports.structured_spec import register_structured_spec_store

import test_architecture_candidates_integration as candidate_fixtures
from test_architecture_candidates_integration import design, read

adopted_context = candidate_fixtures.adopted_context


OPERATIONS = {
    "spec.entity.read",
    "spec.architecture.read",
    "spec.integration_requirements.read",
    "spec.entity.edit_fields",
    "spec.structured_entity.integration_requirement.create",
    "spec.structured_entity.integration_requirement.update",
    "spec.interact_in.draft",
}


def permissions(*, denied=None):
    flags = {}
    for flag in ALL_FLAGS:
        set_permission_flag(flags, flag, flag in OPERATIONS and flag != denied)
    return PermissionSet(flags)


def actor(*, denied=None, source="rest", actor_id="author", **extra):
    return ActorContext(
        actor_id,
        source,
        board_id="board",
        actor_name="Author",
        actor_kind="agent" if source == "mcp" else "human",
        permissions=permissions(denied=denied),
        **extra,
    )


@pytest_asyncio.fixture
async def classified_context(adopted_context):
    db = adopted_context
    register_structured_spec_store(CommunitySqlAlchemyStructuredSpecStore())
    register_domain_event_publisher(CommunitySqlAlchemyDomainEventPublisher())
    # Runtime registries are reset between tests. Restore the real subscriber
    # for the emitted event types, without starting a background dispatcher.
    register_handler(
        "spec.version_bumped", "spec.semantic_changed", "structured_entity.created"
    )(
        ConsolidationEnqueuer,
    )
    db.add_all([design("promote"), design("associate"), design("context")])
    # Existing normative IR stays identical through association/context-only.
    await db.execute(
        update(Spec)
        .where(Spec.id == "spec")
        .values(
            integration_requirements=[
                {
                    "id": "ir_existing",
                    "title": "Consume orders",
                    "integration_type": "event",
                    "status": "active",
                    "description": "Existing obligation",
                }
            ],
        )
    )
    await db.commit()
    return db


async def batch_for(db, *, disposition=None):
    population = await read(db)
    candidates = {item.root_design_id: item for item in population.candidates}
    decisions = []
    for name, payload in (
        (
            "promote",
            {
                "disposition": "promote_to_ir",
                "integration_requirements": [
                    {"title": "Publish orders", "integration_type": "event"},
                    {"title": "Export orders", "integration_type": "file"},
                ],
            },
        ),
        (
            "associate",
            {
                "disposition": "associate_existing_ir",
                "integration_requirement_refs": ["ir_existing"],
            },
        ),
        (
            "context",
            {"disposition": "context_only", "reason": "Informative external interface"},
        ),
    ):
        if disposition and payload["disposition"] != disposition:
            continue
        candidate = candidates[name]
        decisions.append(
            {
                "candidate_ref": candidate.id,
                "expected_source_digest": candidate.source_digest,
                **payload,
            }
        )
    record = await CommunitySqlAlchemyStructuredSpecStore().get(db, spec_id="spec")
    return ArchitectureClassificationBatch.model_validate(
        {
            "expected_spec_version": record.version,
            "expected_spec_edition": record.edition,
            "idempotency_key": "mixed-batch",
            "decisions": decisions,
        }
    )


async def execute(db, batch, *, who=None, spec_id="spec"):
    who = who or actor()
    uow = CommunityUnitOfWork(db, actor=who)
    return await ClassifyArchitectureCandidatesUseCase().execute(
        ClassifyArchitectureCandidatesCommand("board", spec_id, batch),
        actor=who,
        uow=uow,
    )


async def snapshot(db):
    db.expire_all()
    record = await CommunitySqlAlchemyStructuredSpecStore().get(db, spec_id="spec")
    counts = [
        await db.scalar(select(func.count()).select_from(table))
        for table in (
            ArchitectureClassificationReceiptRow,
            ArchitectureCandidateDecisionRow,
            SpecHistory,
            DomainEventRow,
            DomainEventHandlerExecution,
        )
    ]
    return record, counts


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["rest", "mcp"])
async def test_mixed_batch_creates_once_preserves_normative_ir_and_records_actor(
    classified_context, source
):
    db = classified_context
    batch = await batch_for(db)
    before, _ = await snapshot(db)
    result = await execute(db, batch, who=actor(source=source))
    after, counts = await snapshot(db)
    assert result["replayed"] is False
    assert after.version == before.version + 1 == result["spec_version"]
    assert after.edition == before.edition == 2
    assert after.integration_requirements[0] == before.integration_requirements[0]
    assert [item["id"] for item in after.integration_requirements[1:]] == result[
        "created_ir_ids"
    ]
    assert [
        item["integration_type"] for item in after.integration_requirements[1:]
    ] == ["event", "file"]
    assert counts[:4] == [1, 3, 1, 4]
    assert counts[4] > 0
    assert [item["disposition"] for item in result["decisions"]] == [
        "promote_to_ir",
        "associate_existing_ir",
        "context_only",
    ]
    history = (await db.scalars(select(SpecHistory))).one()
    assert (
        history.action == "architecture_classified" and history.version == after.version
    )
    assert history.actor_id == "author" and history.actor_name == "Author"
    assert history.actor_type == ("agent" if source == "mcp" else "user")
    events = (await db.scalars(select(DomainEventRow))).all()
    assert all(
        item.actor_id == "author" and item.actor_type == history.actor_type
        for item in events
    )
    decisions = (
        await CommunitySqlAlchemyStructuredSpecStore().list_architecture_decisions(
            db,
            spec_id="spec",
            spec_edition=2,
        )
    )
    assert len(decisions) == 3
    assert all(item.source_contract_json and item.adopted_sources for item in decisions)
    assert result["pending_checks"] == [
        "requirement_readiness_and_spec_start_gates_not_evaluated"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("denied", sorted(OPERATIONS))
async def test_any_denied_permission_prevents_design_body_reads_and_all_writes(
    classified_context, denied
):
    db = classified_context
    batch = await batch_for(db)
    before = await snapshot(db)
    statements = []

    def capture(_conn, _cursor, sql, *_args):
        statements.append(sql.lower())

    event.listen(db.bind.sync_engine, "before_cursor_execute", capture)
    try:
        with pytest.raises(PermissionDeniedError):
            await execute(db, batch, who=actor(denied=denied))
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", capture)
    assert statements
    assert not any("architecture_designs" in sql for sql in statements)
    assert not any("specs.integration_requirements" in sql for sql in statements)
    assert not any(
        sql.lstrip().startswith(("insert", "update", "delete")) for sql in statements
    )
    assert await snapshot(db) == before


@pytest.mark.asyncio
async def test_context_only_does_not_require_ir_writer_authority_or_modify_irs(
    classified_context,
):
    db = classified_context
    batch = await batch_for(db, disposition="context_only")
    before, _ = await snapshot(db)
    who = actor()
    for op in ("create", "update"):
        set_permission_flag(
            who.permissions.flags,
            f"spec.structured_entity.integration_requirement.{op}",
            False,
        )
    result = await execute(db, batch, who=who)
    after, counts = await snapshot(db)
    assert after.integration_requirements == before.integration_requirements
    assert after.version == before.version + 1
    assert result["created_ir_ids"] == [] and counts[:4] == [1, 1, 1, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,code",
    [
        ("version", "architecture_classification_version_conflict"),
        ("edition", "architecture_classification_version_conflict"),
        ("digest", "architecture_candidate_source_changed"),
        ("invalid_last_ir", "architecture_classification_link_target_invalid"),
        ("missing_ir", "architecture_classification_ir_not_active_in_spec"),
        ("lock", "architecture_classification_spec_locked"),
    ],
)
async def test_invalid_batch_preserves_every_table(classified_context, case, code):
    db = classified_context
    raw = (await batch_for(db)).model_dump(mode="json")
    if case in {"version", "edition"}:
        raw[f"expected_spec_{case}"] += 1
    elif case == "digest":
        raw["decisions"][-1]["expected_source_digest"] = "a" * 64
    elif case == "invalid_last_ir":
        raw["decisions"][0]["integration_requirements"][-1]["linked_api_contracts"] = [
            "api_absent"
        ]
    elif case == "missing_ir":
        raw["decisions"][1]["integration_requirement_refs"] = ["ir_foreign"]
    elif case == "lock":
        await db.execute(
            update(Spec)
            .where(Spec.id == "spec")
            .values(
                current_validation_id="validation",
                validations=[{"id": "validation", "outcome": "success", "edition": 2}],
            )
        )
        await db.commit()
    before = await snapshot(db)
    with pytest.raises(ArchitectureClassificationError, match=f"^{code}$"):
        await execute(db, ArchitectureClassificationBatch.model_validate(raw))
    assert await snapshot(db) == before


@pytest.mark.asyncio
async def test_draft_gate_survives_unrestricted_permissions(classified_context):
    db = classified_context
    batch = await batch_for(db)
    await db.execute(update(Spec).where(Spec.id == "spec").values(status="in_progress"))
    await db.commit()
    before = await snapshot(db)
    who = actor()
    set_permission_flag(who.permissions.flags, "spec.interact_in.in_progress", True)
    with pytest.raises(SubjectEditRequiresDraftError):
        await execute(db, batch, who=who)
    assert await snapshot(db) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_table", ["domain_events", "spec_history"])
async def test_outbox_or_history_failure_rolls_back_already_flushed_receipt_and_irs(
    classified_context, failure_table
):
    db = classified_context
    batch = await batch_for(db)
    before = await snapshot(db)

    def fail_write(_conn, _cursor, sql, *_args):
        if sql.lower().startswith(f"insert into {failure_table}"):
            raise RuntimeError("injected transactional failure")

    event.listen(db.bind.sync_engine, "before_cursor_execute", fail_write)
    try:
        with pytest.raises(RuntimeError, match="injected transactional failure"):
            await execute(db, batch)
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", fail_write)
    assert await snapshot(db) == before
    # Failed attempts do not consume the key or the expected version.
    assert (await execute(db, batch))["replayed"] is False


@pytest.mark.asyncio
async def test_replay_is_actor_bound_read_only_and_still_checks_current_permissions(
    classified_context,
):
    db = classified_context
    batch = await batch_for(db)
    first = await execute(db, batch)
    await db.execute(
        update(ArchitectureDesign)
        .where(ArchitectureDesign.id == "promote")
        .values(
            interfaces=[{"id": "boundary", "event_schema": {"const": "changed"}}],
            version=2,
        )
    )
    await db.commit()
    before = await snapshot(db)
    statements = []

    def capture(_conn, _cursor, sql, *_args):
        statements.append(sql.lower())

    event.listen(db.bind.sync_engine, "before_cursor_execute", capture)
    try:
        assert await execute(db, batch) == {**first, "replayed": True}
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", capture)
    assert not any(
        sql.lstrip().startswith(("insert", "update", "delete")) for sql in statements
    )
    assert not any("architecture_designs" in sql for sql in statements)
    assert await snapshot(db) == before
    with pytest.raises(PermissionDeniedError):
        await execute(db, batch, who=actor(denied="spec.entity.edit_fields"))
    with pytest.raises(ArchitectureClassificationError, match="idempotency_conflict"):
        # A request from a different authenticated identity owns a different
        # physical session; the semantic actor binding cannot be reassigned.
        async with async_sessionmaker(db.bind, expire_on_commit=False)() as other_db:
            await execute(
                other_db, batch, who=actor(source="mcp", actor_id="other-author")
            )
    raw = batch.model_dump(mode="json")
    raw["decisions"][-1]["reason"] = "Different intent with the same key"
    with pytest.raises(ArchitectureClassificationError, match="idempotency_conflict"):
        await execute(db, ArchitectureClassificationBatch.model_validate(raw))
    assert await snapshot(db) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["realm", "foreign_spec", "missing_spec"])
async def test_non_enumerable_scope_rejects_without_changes(classified_context, case):
    db = classified_context
    batch = await batch_for(db)
    before = await snapshot(db)
    who = actor(realm_id="tenant-elsewhere") if case == "realm" else actor()
    target = {"realm": "spec", "foreign_spec": "other-spec", "missing_spec": "absent"}[
        case
    ]
    with pytest.raises(EntityNotFoundError):
        await execute(db, batch, who=who, spec_id=target)
    assert await snapshot(db) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [None, tuple(sorted(OPERATIONS))])
async def test_canonical_legacy_mcp_authority_is_reified_without_mutating_policy(
    classified_context, legacy
):
    db = classified_context
    batch = await batch_for(db)
    who = actor(source="mcp")
    who.permissions = copy.deepcopy(legacy)
    result = await execute(db, batch, who=who)
    assert result["replayed"] is False and len(result["created_ir_ids"]) == 2
    assert who.permissions == legacy


@pytest.mark.asyncio
async def test_concurrent_exact_requests_share_receipt_and_one_version_bump(
    classified_context,
):
    db = classified_context
    batch = await batch_for(db)
    before, _ = await snapshot(db)
    await db.rollback()
    sessions = async_sessionmaker(
        db.bind,
        expire_on_commit=False,
        sync_session_class=CommunitySemanticSession,
    )

    async def request():
        async with sessions() as request_db:
            return await execute(request_db, batch)

    results = await asyncio.gather(request(), request())
    assert sorted(item["replayed"] for item in results) == [False, True]
    assert {**results[0], "replayed": True} == {**results[1], "replayed": True}
    after, counts = await snapshot(db)
    assert after.version == before.version + 1
    assert (
        len(after.integration_requirements) == len(before.integration_requirements) + 2
    )
    assert counts[:4] == [1, 3, 1, 4]


@pytest.mark.asyncio
async def test_owner_review_required_cannot_be_bypassed_by_classification(
    classified_context,
):
    db = classified_context
    batch = await batch_for(db)
    before = await snapshot(db)
    who = actor()
    who.permissions = PermissionSet(who.permissions.flags, owner_review_required=True)
    with pytest.raises(PermissionDeniedError):
        await execute(db, batch, who=who)
    assert await snapshot(db) == before
