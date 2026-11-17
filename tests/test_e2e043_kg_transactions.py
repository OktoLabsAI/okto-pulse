"""R3: a paused native graph commit must not reserve SQLite's writer."""

import asyncio
from datetime import datetime, timezone
from threading import Event

import pytest
from sqlalchemy import insert, select, text, update

from okto_pulse.core.application.processors.consolidation import ConsolidationProcessor
from okto_pulse.community.adapters.grafx_graph_transaction import CommunityGrafxGraphTransaction
from okto_pulse.community.adapters.sqlalchemy_consolidation import CommunitySqlAlchemyConsolidationPersistence
from okto_pulse.community.adapters.sqlalchemy_models import (
    Board, Card, ConsolidationAudit, ConsolidationQueue, Ideation, Refinement, Spec, Story, Topic,
)
from test_projection_materialized_parity import materialize
from test_code_traceability_kg_rebuild_e2e import seed_complete_traceability_source


@pytest.mark.asyncio
@pytest.mark.timeout(180)
@pytest.mark.parametrize("artifact_type", ["refinement", "ideation", "story", "code_evidence"])
async def test_native_commit_releases_sql_writer_before_graph_wait(tmp_path, monkeypatch, artifact_type):
    model = {"refinement": Refinement, "ideation": Ideation, "story": Story}.get(artifact_type)
    artifact_id = {"ideation": "idea", "code_evidence": "evidence-1"}.get(artifact_type, artifact_type)
    async def seed(factory):
        async with factory() as session:
            if artifact_type == "code_evidence":
                await seed_complete_traceability_source(session, board_id="board", spec_id="spec", card_id="card",
                    requirement_id="fr_one", include_parents=False, now=datetime.now(timezone.utc), link_spec_version=2)
                await session.execute(update(Spec).where(Spec.id == "spec").values(version=2))
            await session.execute(insert(Ideation).values(
                id="idea", board_id="board", title="Idea", status="done", created_by="owner",
            ))
            await session.execute(insert(Refinement).values(
                id="refinement", ideation_id="idea", board_id="board", title="Refinement",
                status="done", created_by="owner", analysis="Bounded worker transaction analysis",
            ))
            await session.execute(insert(Topic).values(
                id="topic", board_id="board", name="Transaction tests", created_by="owner",
            ))
            await session.execute(insert(Story).values(
                id="story", board_id="board", topic_id="topic", title="Story", description="Story for native transaction isolation",
                status="ready", created_by="owner",
            ))
            await session.commit()

    async def exercise(factory, bundle):
        async with factory() as session:
            if model is not None:
                artifact = await session.get(model, artifact_id)
                artifact.title = f"Changed {artifact_type}"
                if hasattr(artifact, "version"):
                    artifact.version += 1
            session.add(ConsolidationQueue(
                id="write-boundary", board_id="board", artifact_type=artifact_type,
                artifact_id=artifact_id, source="state_transition",
            ))
            await session.commit()

        entered, release = Event(), Event()
        original = CommunityGrafxGraphTransaction.begin

        class PausedCommit:
            def __init__(self, scope):
                self.scope = scope

            def __getattr__(self, name):
                return getattr(self.scope, name)

            async def __aenter__(self):
                await self.scope.__aenter__()
                return self

            async def __aexit__(self, *args):
                return await self.scope.__aexit__(*args)

            async def commit(self):
                entered.set()
                if not await asyncio.to_thread(release.wait, 20):
                    raise TimeoutError("test did not release graph commit")
                return await self.scope.commit()

        async def begin(self, *args, **kwargs):
            return PausedCommit(await original(self, *args, **kwargs))

        monkeypatch.setattr(CommunityGrafxGraphTransaction, "begin", begin)
        processor = ConsolidationProcessor(
            relational_scope_factory=factory, durable_projection_protocol=True,
        )
        worker = asyncio.create_task(processor.process_batch())
        write_error = None
        try:
            assert await asyncio.to_thread(entered.wait, 30)
            try:
                async with factory() as session:
                    await session.execute(text("PRAGMA busy_timeout=250"))
                    session.add(Board(id="unrelated", name="Independent writer", owner_id="owner", realm_id="local"))
                    await session.commit()
                assert not release.is_set() and not worker.done()
            except Exception as exc:
                write_error = exc
        finally:
            release.set()
            monkeypatch.setattr(CommunityGrafxGraphTransaction, "begin", original)
        processed = await worker
        assert write_error is None, f"SQLite writer held across graph IO: {write_error}"
        assert processed == 1
        async with factory() as session:
            assert await session.get(ConsolidationQueue, "write-boundary") is None
            assert await session.get(Board, "unrelated") is not None
            audits = (await session.execute(select(ConsolidationAudit).where(
                ConsolidationAudit.artifact_id == artifact_id,
            ))).scalars().all()
            assert audits

        # Native graph commit succeeds; SQLite finalization fails. Recovery
        # must settle the prepared operation once, retaining the original audit.
        async with factory() as session:
            if model is not None:
                artifact = await session.get(model, artifact_id)
                artifact.title = f"Recoverable {artifact_type}"
                if hasattr(artifact, "version"):
                    artifact.version += 1
            session.add(ConsolidationQueue(id="after-native-commit", board_id="board",
                artifact_type=artifact_type, artifact_id=artifact_id, source="state_transition"))
            await session.commit()
        original_finalize = CommunitySqlAlchemyConsolidationPersistence.finalize_projection_operation

        async def fail_finalize(*args, **kwargs):
            raise RuntimeError("injected_after_native_commit")

        monkeypatch.setattr(CommunitySqlAlchemyConsolidationPersistence, "finalize_projection_operation", fail_finalize)
        try:
            assert await processor.process_batch() == 0
        finally:
            monkeypatch.setattr(CommunitySqlAlchemyConsolidationPersistence, "finalize_projection_operation", original_finalize)
        assert await processor.recover_projection_operations() == 1
        async with factory() as session:
            assert await session.get(ConsolidationQueue, "after-native-commit") is None
            audits = (await session.execute(select(ConsolidationAudit).where(
                ConsolidationAudit.artifact_id == artifact_id,
            ))).scalars().all()
            audit_count = len(audits)
        assert await processor.recover_projection_operations() == 0
        async with factory() as session:
            assert len((await session.execute(select(ConsolidationAudit).where(
                ConsolidationAudit.artifact_id == artifact_id,
            ))).scalars().all()) == audit_count
            session.add(ConsolidationQueue(id="unchanged-replay", board_id="board",
                artifact_type=artifact_type, artifact_id=artifact_id, source="state_transition"))
            await session.commit()
        assert await processor.process_batch() == 1
        async with factory() as session:
            assert await session.get(ConsolidationQueue, "unchanged-replay") is None

    await materialize(tmp_path / f"{artifact_type}-writer", incremental=False, seed=seed, exercise=exercise,
        card_type="normal" if artifact_type == "code_evidence" else None)


@pytest.mark.asyncio
@pytest.mark.timeout(180)
async def test_cancelled_test_card_replay_keeps_native_ack_truthful(tmp_path, monkeypatch):
    async def seed(factory):
        async with factory() as session:
            await session.execute(update(Card).where(Card.id == "card").values(
                status="cancelled", conclusions=None,
                cancellation_reason="Controlled regression probe completed; preserve its audit history.",
            ))
            await session.commit()

    async def exercise(factory, graph):
        processor = ConsolidationProcessor(relational_scope_factory=factory, durable_projection_protocol=True)
        for iteration in range(2):
            queue_id = f"cancelled-probe-{iteration}"
            async with factory() as session:
                session.add(ConsolidationQueue(id=queue_id, board_id="board", artifact_type="card",
                    artifact_id="card", source="state_transition"))
                await session.commit()
            processed = await processor.process_batch()
            async with factory() as session:
                pending = await session.get(ConsolidationQueue, queue_id)
                assert processed == 1 and pending is None, getattr(pending, "last_error", None)
                assert (await session.get(Card, "card")).status.value == "cancelled"
                assert not await CommunitySqlAlchemyConsolidationPersistence().list_unsettled_projection_operations(
                    session, board_id="board")

        # An audit staged for an unchanged graph must roll back with a failed
        # ACK. A later retry records it once without a native commit marker.
        async with factory() as session:
            audits_before = len((await session.execute(select(ConsolidationAudit))).scalars().all())
            session.add(ConsolidationQueue(id="noop-ack-failure", board_id="board", artifact_type="card",
                artifact_id="card", source="state_transition"))
            await session.commit()
        original_ack = CommunitySqlAlchemyConsolidationPersistence.ack_claimed_queue_entry

        async def fail_ack(*args, **kwargs):
            raise RuntimeError("injected_noop_ack_failure")

        monkeypatch.setattr(CommunitySqlAlchemyConsolidationPersistence, "ack_claimed_queue_entry", fail_ack)
        try:
            assert await processor.process_batch() == 0
        finally:
            monkeypatch.setattr(CommunitySqlAlchemyConsolidationPersistence, "ack_claimed_queue_entry", original_ack)
        async with factory() as session:
            assert len((await session.execute(select(ConsolidationAudit))).scalars().all()) == audits_before
            assert await session.get(ConsolidationQueue, "noop-ack-failure") is not None
            assert not await CommunitySqlAlchemyConsolidationPersistence().list_unsettled_projection_operations(
                session, board_id="board")
            # Advance only this isolated fixture's retry time; no live queue is edited.
            await session.execute(update(ConsolidationQueue).where(
                ConsolidationQueue.id == "noop-ack-failure").values(next_retry_at=None))
            await session.commit()
        assert await processor.process_batch() == 1
        async with factory() as session:
            assert await session.get(ConsolidationQueue, "noop-ack-failure") is None
            assert len((await session.execute(select(ConsolidationAudit))).scalars().all()) == audits_before + 1

    await materialize(tmp_path / "cancelled-test", incremental=False, card_type="test", seed=seed, exercise=exercise)
