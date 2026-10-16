"""KG-16/18: Target evidence retraction is scoped to its relational owner."""
import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, insert, select, update

from okto_pulse.core.application.processors.consolidation import ConsolidationProcessor
from okto_pulse.community.adapters.sqlalchemy_models import Base, ConsolidationQueue, ConsolidationAudit, Spec
from okto_pulse.community.adapters.grafx_graph_transaction import _GrafxTransactionScope
from test_code_traceability_kg_rebuild_e2e import seed_complete_traceability_source
from test_projection_materialized_parity import materialize, relationship_set


@pytest.mark.asyncio
@pytest.mark.timeout(300)
@pytest.mark.parametrize("fail_cleanup", [False, True])
async def test_target_retracts_evidence_without_removing_neighbours_claim(
        tmp_path, monkeypatch, fail_cleanup):
    targets = Base.metadata.tables["implementation_targets"]
    links = Base.metadata.tables["implementation_target_evidence_links"]
    sequence = 0

    async def project(factory, *owners, drain=True):
        nonlocal sequence
        async with factory() as session:
            for kind, identity in owners:
                sequence += 1
                session.add(ConsolidationQueue(id=f"target-owner-{sequence}", board_id="board",
                    artifact_type=kind, artifact_id=identity, source="state_transition"))
            await session.commit()
        if not drain:
            return
        deadline = asyncio.get_running_loop().time() + 90
        while asyncio.get_running_loop().time() < deadline:
            async with factory() as session:
                pending = (await session.execute(select(ConsolidationQueue.artifact_id,
                    ConsolidationQueue.status, ConsolidationQueue.last_error))).all()
            if not pending:
                return
            await ConsolidationProcessor(relational_scope_factory=factory).process_batch()
            await asyncio.sleep(0.1)
        pytest.fail(f"Target projection did not converge: {pending}")

    def facts(graph):
        return {edge: count for edge, count in relationship_set(graph).items()
            if edge[3] == "derives_from/code_traceability_evidence@v2.0"}

    async def exercise(factory, graph):
        async with factory() as session:
            await seed_complete_traceability_source(session, board_id="board",
                spec_id="spec", card_id="card", requirement_id="fr_one",
                include_parents=False, now=datetime.now(timezone.utc), link_spec_version=2)
            (await session.get(Spec, "spec")).version = 2
            target = dict((await session.execute(select(targets))).mappings().one())
            neighbour = {**target, "id": "target-2", "relative_path_hint": "src/other.py",
                "current_resolution_id": None}
            await session.execute(insert(targets).values(neighbour))
            await session.commit()
        await project(factory, ("code_investigation_receipt", "receipt-1"),
            ("code_evidence", "evidence-1"), ("implementation_target", "target-1"),
            ("implementation_target", "target-2"))
        initial = facts(graph)
        assert len(initial) == 2 and set(initial.values()) == {1}
        assert {edge[1] for edge in initial} == {
            "implementation_target:target-1", "implementation_target:target-2"}
        assert {edge[2] for edge in initial} == {"code_evidence:evidence-1"}
        async with factory() as session:
            await session.execute(delete(links).where(links.c.target_id == "target-1"))
            await session.execute(update(targets).where(targets.c.id == "target-1").values(
                baseline_evidence_id=None, revision=2, current_resolution_id=None,
                last_change_reason_sha256="c" * 64))
            await session.commit()
        if fail_cleanup:
            before_failure = relationship_set(graph)
            async with factory() as session:
                audits = (await session.execute(select(ConsolidationAudit.session_id))).scalars().all()
            await project(factory, ("implementation_target", "target-1"), drain=False)
            mutate, reached = _GrafxTransactionScope._mutation, []
            def fail_after_delete(scope, *args, **kwargs):
                result = mutate(scope, *args, **kwargs)
                if kwargs.get("operation") == "delete_projection_spec_relationship_edge":
                    reached.append(True)
                    raise RuntimeError("injected target evidence cleanup failure")
                return result
            with monkeypatch.context() as patch:
                patch.setattr(_GrafxTransactionScope, "_mutation", fail_after_delete)
                assert await ConsolidationProcessor(relational_scope_factory=factory).process_batch() == 0
            assert reached == [True]
            assert relationship_set(graph) == before_failure
            async with factory() as session:
                assert (await session.execute(select(ConsolidationAudit.session_id))).scalars().all() == audits
                pending = (await session.execute(select(ConsolidationQueue))).scalars().one()
                assert pending.artifact_id == "target-1" and pending.last_error
            await project(factory)
        else:
            await project(factory, ("implementation_target", "target-1"))
        assert facts(graph) == {edge: count for edge, count in initial.items()
            if edge[1] == "implementation_target:target-2"}
        after = relationship_set(graph)
        await project(factory, ("implementation_target", "target-1"))
        assert relationship_set(graph) == after
        async with factory() as session:
            assert dict((await session.execute(select(targets)
                .where(targets.c.id == "target-2"))).mappings().one()) == neighbour

    await materialize(tmp_path / "target-evidence", incremental=False,
        card_type="normal", exercise=exercise, native_schema=True)
