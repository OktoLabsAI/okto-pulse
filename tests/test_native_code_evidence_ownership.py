"""KG-18: retract one native Evidence claim without touching its neighbour."""
import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, insert, select

from okto_pulse.core.application.processors.consolidation import ConsolidationProcessor
from okto_pulse.community.adapters.sqlalchemy_models import Base, ConsolidationQueue, ConsolidationAudit, Spec
from okto_pulse.community.adapters.grafx_graph_transaction import _GrafxTransactionScope

from test_code_traceability_kg_rebuild_e2e import seed_complete_traceability_source
from test_projection_materialized_parity import materialize, relationship_set


@pytest.mark.asyncio
@pytest.mark.timeout(300)
@pytest.mark.parametrize("fail_cleanup", [False, True])
async def test_unlink_one_evidence_preserves_adjacent_owner_and_source_content(
        tmp_path, monkeypatch, fail_cleanup):
    evidence = Base.metadata.tables["code_evidence"]
    links = Base.metadata.tables["code_evidence_spec_links"]
    rule = "supports/code_traceability_spec_link@v2.0"
    serial = 0

    async def project(factory, *owners, drain=True):
        nonlocal serial
        async with factory() as session:
            for kind, identity in owners:
                serial += 1
                session.add(ConsolidationQueue(id=f"evidence-owner-{serial}",
                    board_id="board", artifact_type=kind, artifact_id=identity,
                    source="state_transition"))
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
        pytest.fail(f"Code Evidence did not converge: {pending}")

    def claims(graph):
        return {edge: count for edge, count in relationship_set(graph).items()
            if edge[3] == rule}

    async def exercise(factory, graph):
        async with factory() as session:
            await seed_complete_traceability_source(session, board_id="board",
                spec_id="spec", card_id="card", requirement_id="fr_one",
                include_parents=False, now=datetime.now(timezone.utc), link_spec_version=2)
            original = dict((await session.execute(select(evidence)
                .where(evidence.c.id == "evidence-1"))).mappings().one())
            await session.execute(insert(evidence).values({**original,
                "id": "evidence-2", "claim": "An independent observation.",
                "idempotency_key": "evidence-idempotency-2"}))
            link = dict((await session.execute(select(links))).mappings().one())
            await session.execute(insert(links).values({**link,
                "id": "evidence-spec-link-2", "evidence_id": "evidence-2"}))
            (await session.get(Spec, "spec")).version = 2
            await session.commit()
        await project(factory, ("code_investigation_receipt", "receipt-1"),
            ("code_evidence", "evidence-1"), ("code_evidence", "evidence-2"))
        before = claims(graph)
        assert len(before) == 2 and set(before.values()) == {1}
        assert {edge[1] for edge in before} == {
            "code_evidence:evidence-1", "code_evidence:evidence-2"}
        assert {edge[2] for edge in before} == {"spec:spec:fr:fr_one"}
        async with factory() as session:
            source_before = [dict(row) for row in
                (await session.execute(select(evidence).order_by(evidence.c.id))).mappings()]
            await session.execute(delete(links).where(links.c.evidence_id == "evidence-1"))
            await session.commit()
        if fail_cleanup:
            before_failure = relationship_set(graph)
            async with factory() as session:
                audits = (await session.execute(select(ConsolidationAudit.session_id))).scalars().all()
            await project(factory, ("code_evidence", "evidence-1"), drain=False)
            mutate = _GrafxTransactionScope._mutation
            reached = []
            def fail_after_delete(scope, *args, **kwargs):
                result = mutate(scope, *args, **kwargs)
                if kwargs.get("operation") == "delete_projection_spec_relationship_edge":
                    reached.append(True)
                    raise RuntimeError("injected evidence cleanup failure")
                return result
            with monkeypatch.context() as patch:
                patch.setattr(_GrafxTransactionScope, "_mutation", fail_after_delete)
                assert await ConsolidationProcessor(relational_scope_factory=factory).process_batch() == 0
            assert reached == [True]
            assert relationship_set(graph) == before_failure
            async with factory() as session:
                assert (await session.execute(select(ConsolidationAudit.session_id))).scalars().all() == audits
                pending = (await session.execute(select(ConsolidationQueue))).scalars().one()
                assert pending.artifact_id == "evidence-1"
                assert pending.last_error
            await project(factory)
        else:
            await project(factory, ("code_evidence", "evidence-1"))
        expected = {edge: count for edge, count in before.items()
            if edge[1] == "code_evidence:evidence-2"}
        assert claims(graph) == expected
        after = relationship_set(graph)
        await project(factory, ("code_evidence", "evidence-1"))
        assert relationship_set(graph) == after
        async with factory() as session:
            assert [dict(row) for row in
                (await session.execute(select(evidence).order_by(evidence.c.id))).mappings()
                ] == source_before
            assert (await session.execute(select(links.c.evidence_id))).scalars().all() == [
                "evidence-2"]

    await materialize(tmp_path / "evidence-owners", incremental=False,
        card_type="normal", exercise=exercise, native_schema=True)
