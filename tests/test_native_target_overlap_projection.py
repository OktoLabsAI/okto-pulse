"""KG-16/24: current overlap must disappear when its attested basis disappears."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import insert, select, update

from okto_pulse.core.application.processors.consolidation import ConsolidationProcessor
from okto_pulse.core.ports.code_traceability import TargetOverlapQuery
from okto_pulse.community.adapters.sqlalchemy_code_traceability import CommunitySqlAlchemyCodeTraceabilityStore
from okto_pulse.community.adapters.sqlalchemy_models import Base, Card, Spec, ConsolidationQueue
from test_code_traceability_kg_rebuild_e2e import seed_complete_traceability_source
from test_projection_materialized_parity import materialize, relationship_set
from test_skb3_semantic_guideline_persistence import semantic_relational_application_adapter  # noqa: F401


async def seed_overlapping_targets(factory, *, now=None):
    now = now or datetime.now(timezone.utc)
    tables = Base.metadata.tables
    async with factory() as session:
        await seed_complete_traceability_source(session, board_id="board",
            spec_id="spec", card_id="card", requirement_id="fr_one",
            include_parents=False, now=now, link_spec_version=2)
        (await session.get(Spec, "spec")).version = 2
        session.add(Card(id="card-2", board_id="board", spec_id="spec", title="Other Card",
            status="done", card_type="normal", created_by="owner",
            test_scenario_ids=[], conclusions=[{"summary": "Completed"}]))
        await session.flush()

        async def one(name):
            return dict((await session.execute(select(tables[name]))).mappings().one())

        request = await one("code_investigation_requests")
        receipt = await one("code_investigation_receipts")
        target = await one("implementation_targets")
        resolution = await one("implementation_target_resolutions")
        await session.execute(insert(tables["code_investigation_requests"]).values({
            **request, "id": "request-2", "subject_id": "card-2", "status": "open",
            "consumed_at": None, "expected_head_generation": 1, "challenge_token_hash": "d" * 64,
            "expected_predecessor_receipt_id": "receipt-1",
            "idempotency_key": "request-idempotency-2"}))
        await session.execute(insert(tables["code_investigation_receipts"]).values({
            **receipt, "id": "receipt-2", "request_id": "request-2", "subject_id": "card-2",
            "generation": 2, "predecessor_receipt_id": "receipt-1",
            "idempotency_key": "receipt-idempotency-2"}))
        await session.execute(update(tables["code_investigation_requests"])
            .where(tables["code_investigation_requests"].c.id == "request-2")
            .values(status="consumed", consumed_at=now + timedelta(seconds=1)))
        await session.execute(update(tables["code_investigation_heads"]).values(
            generation=2, latest_receipt_id="receipt-2", current_receipt_id="receipt-2", revision=2))
        await session.execute(insert(tables["implementation_targets"]).values({
            **target, "id": "target-2", "card_id": "card-2",
            "baseline_evidence_id": None, "current_resolution_id": None}))
        await session.execute(insert(tables["implementation_target_resolutions"]).values({
            **resolution, "id": "resolution-2", "target_id": "target-2",
            "investigation_receipt_id": "receipt-2", "receipt_generation": 2,
            "idempotency_key": "resolution-idempotency-2"}))
        await session.execute(update(tables["implementation_targets"])
            .where(tables["implementation_targets"].c.id == "target-2")
            .values(current_resolution_id="resolution-2"))
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.timeout(300)
@pytest.mark.parametrize("fail_cleanup", [False, True])
async def test_overlap_retracts_after_current_resolution_is_invalidated(tmp_path, monkeypatch, fail_cleanup):
    targets = Base.metadata.tables["implementation_targets"]

    async def exercise(factory, graph):
        rule = "overlaps/code_traceability_current@v2.0"
        initial = relationship_set(graph)
        overlaps = {edge: count for edge, count in initial.items() if edge[3] == rule}
        assert len(overlaps) == 1 and set(overlaps.values()) == {1}
        assert {edge[1:3] for edge in overlaps} == {
            ("implementation_target:target-1", "implementation_target:target-2")}
        async with factory() as session:
            peer = dict((await session.execute(select(targets)
                .where(targets.c.id == "target-1"))).mappings().one())
            await session.execute(update(targets).where(targets.c.id == "target-2").values(
                revision=2, current_resolution_id=None, last_change_reason_sha256="c" * 64))
            await session.commit()
            assert await CommunitySqlAlchemyCodeTraceabilityStore(session).overlap_report(
                TargetOverlapQuery(board_id="board", card_id="card", include_informational=True)) == ()
        # Process both endpoints: this first proves reconciliation independent
        # of the separate causal invalidation requirement.
        previous_projection = None
        for replay in range(2):
            for identity in ("target-2", "target-1"):
                async with factory() as session:
                    session.add(ConsolidationQueue(id=f"overlap-{replay}-{identity}", board_id="board",
                        artifact_type="implementation_target", artifact_id=identity, source="state_transition"))
                    await session.commit()
                if fail_cleanup and replay == 0 and identity == "target-1":
                    from okto_pulse.community.adapters.grafx_graph_transaction import _GrafxTransactionScope
                    from okto_pulse.community.adapters.sqlalchemy_models import ConsolidationAudit
                    before_failure = relationship_set(graph)
                    async with factory() as session:
                        audits = (await session.scalars(select(ConsolidationAudit.session_id))).all()
                    mutate, reached = _GrafxTransactionScope._mutation, []
                    def fail_after_delete(scope, *args, **kwargs):
                        value = mutate(scope, *args, **kwargs)
                        if kwargs.get("operation") == "delete_projection_spec_relationship_edge":
                            reached.append(True)
                            raise RuntimeError("injected overlap deletion failure")
                        return value
                    with monkeypatch.context() as patch:
                        patch.setattr(_GrafxTransactionScope, "_mutation", fail_after_delete)
                        assert await ConsolidationProcessor(relational_scope_factory=factory).process_batch() == 0
                    assert reached == [True]
                    assert relationship_set(graph) == before_failure
                    async with factory() as session:
                        assert (await session.scalars(select(ConsolidationAudit.session_id))).all() == audits
                        pending = (await session.scalars(select(ConsolidationQueue))).one()
                        assert pending.artifact_id == "target-1" and pending.last_error
                        pending.next_retry_at = None
                        await session.commit()
                assert await ConsolidationProcessor(relational_scope_factory=factory).process_batch() == 1
            current = relationship_set(graph)
            assert not [edge for edge in current if edge[3] == rule]
            if replay:
                assert current == previous_projection
            previous_projection = current
        async with factory() as session:
            assert dict((await session.execute(select(targets)
                .where(targets.c.id == "target-1"))).mappings().one()) == peer

    await materialize(tmp_path / "overlap", incremental=False, card_type="normal",
        seed=seed_overlapping_targets, exercise=exercise, native_schema=True)


@pytest.mark.asyncio
@pytest.mark.timeout(300)
@pytest.mark.usefixtures("semantic_relational_application_adapter")
@pytest.mark.parametrize("lifecycle,fail_publish", [
    pytest.param("active", False, id="updated"),
    pytest.param("active", True, id="rollback"),
    pytest.param("revoked", False, id="revoked"),
])
async def test_update_event_preserves_disappeared_pair_owner_and_replays_exactly(tmp_path, monkeypatch, lifecycle, fail_publish):
    from okto_pulse.core.application.use_cases.base import ActorContext
    from okto_pulse.core.application.use_cases.code_traceability import UpdateImplementationTargetUseCase
    from okto_pulse.core.events.handlers.consolidation_enqueuer import ConsolidationEnqueuer
    from okto_pulse.core.events.types import ImplementationTargetUpdated, ImplementationTargetRevoked
    from okto_pulse.core.models.code_traceability import ImplementationTargetUpdateInput
    from okto_pulse.core.services.implementation_targets import ImplementationTargetService
    from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
    from okto_pulse.community.adapters.sqlalchemy_models import DomainEventRow

    event_class = ImplementationTargetRevoked if lifecycle == "revoked" else ImplementationTargetUpdated
    targets = Base.metadata.tables["implementation_targets"]
    actor = ActorContext("owner", "rest", actor_kind="user", board_id="board",
        permissions=("code_traceability.target.edit",))

    async def seed(factory):
        await seed_overlapping_targets(factory)
        async with factory() as session:
            await session.execute(update(Card).where(Card.id == "card-2").values(status="in_progress"))
            peer = dict((await session.execute(select(targets)
                .where(targets.c.id == "target-1"))).mappings().one())
            await session.execute(insert(targets).values({**peer, "id": "target-unrelated",
                "current_resolution_id": None, "baseline_evidence_id": None,
                "relative_path_hint": "src/unrelated.py"}))
            await session.commit()

    async def exercise(factory, graph):
        before = relationship_set(graph)
        async with factory() as session:
            unrelated = dict((await session.execute(select(targets)
                .where(targets.c.id == "target-unrelated"))).mappings().one())
        command = ImplementationTargetUpdateInput(board_id="board", card_id="card-2",
            target_id="target-2", expected_revision=1, lifecycle_status=lifecycle,
            intent="Revise the implementation intent", change_reason="Current scope changed")
        if fail_publish:
            from okto_pulse.core.application.service_catalog import CoreApplicationServiceCatalog
            original_publish = CoreApplicationServiceCatalog.publish_domain_event
            reached = []
            async def reject_after_staging(catalog, event):
                await original_publish(catalog, event)
                reached.append(event)
                raise RuntimeError("injected after overlap event staging")
            with monkeypatch.context() as patch:
                patch.setattr(CoreApplicationServiceCatalog, "publish_domain_event", reject_after_staging)
                with pytest.raises(RuntimeError, match="injected after overlap event staging"):
                    async with CommunityUnitOfWork(factory(), actor=actor) as uow:
                        await UpdateImplementationTargetUseCase(ImplementationTargetService()).execute(
                            command, actor=actor, uow=uow)
            assert len(reached) == 1 and reached[0].overlap_projection_owner_ids == ("target-1",)
            assert relationship_set(graph) == before
            async with factory() as session:
                target = (await session.execute(select(targets).where(targets.c.id == "target-2"))).mappings().one()
                assert target["revision"] == 1 and target["current_resolution_id"] == "resolution-2"
                assert not (await session.scalars(select(DomainEventRow.id).where(
                    DomainEventRow.event_type == event_class.event_type))).all()
                assert not (await session.scalars(select(ConsolidationQueue.id))).all()
        async with CommunityUnitOfWork(factory(), actor=actor) as uow:
            result = await UpdateImplementationTargetUseCase(ImplementationTargetService()).execute(
                command, actor=actor, uow=uow)
            assert result.target.revision == 2 and result.target.current_resolution_id is None
        async with factory() as session:
            row, = (await session.scalars(select(DomainEventRow).where(
                DomainEventRow.event_type == event_class.event_type))).all()
            event = event_class.model_validate({
                **row.payload_json, "event_id": row.id, "board_id": row.board_id,
                "actor_id": row.actor_id, "actor_type": row.actor_type,
                "occurred_at": row.occurred_at})
            assert event.overlap_projection_owner_ids == ("target-1",)
            assert await CommunitySqlAlchemyCodeTraceabilityStore(session).overlap_report(
                TargetOverlapQuery(board_id="board", card_id="card", include_informational=True)) == ()
        previous = None
        for _ in range(2):
            async with factory() as session:
                await ConsolidationEnqueuer().handle(event, session)
                await session.commit()
                owners = (await session.execute(select(ConsolidationQueue.artifact_id))).scalars().all()
                assert sorted(owners) == ["target-1", "target-2"]
            for _owner in owners:
                assert await ConsolidationProcessor(relational_scope_factory=factory).process_batch() == 1
            current = relationship_set(graph)
            assert not [edge for edge in current if edge[3] == "overlaps/code_traceability_current@v2.0"]
            assert {edge: count for edge, count in current.items() if edge[1] == "implementation_target:target-unrelated"} == {
                edge: count for edge, count in before.items() if edge[1] == "implementation_target:target-unrelated"}
            if previous is not None:
                assert current == previous
            previous = current
        async with factory() as session:
            assert dict((await session.execute(select(targets)
                .where(targets.c.id == "target-unrelated"))).mappings().one()) == unrelated

    await materialize(tmp_path / "overlap-event", incremental=False, card_type="normal",
        seed=seed, exercise=exercise, native_schema=True)
