"""Learning policy through native storage and real lifecycle/delivery gates."""
import pytest
from sqlalchemy import select, text

from okto_pulse.community.adapters.sqlalchemy_database import build_community_session_factory
from okto_pulse.community.adapters.sqlalchemy_models import Board, Card, CardDeliveryEvidenceRecordRow, DomainEventRow
from okto_pulse.core.application.use_cases.delivery_evidence import RecordCardDeliveryEvidenceUseCase
from test_adopted_delivery_report import ledger as _ledger, setup, request, BOARD

ledger = _ledger
pytestmark = [pytest.mark.asyncio, pytest.mark.parametrize("ledger", ["native_bug"], indirect=True)]


@pytest.mark.parametrize("skip_cognitive", [False, True])
@pytest.mark.parametrize("policy,contribution", [
    ("advisory", "complete"), ("advisory", "missing"), ("blocking", "complete"),
])
async def test_native_bug_closeout_requires_learning_policy_and_real_delivery(
    ledger, tmp_path, monkeypatch, policy, contribution, skip_cognitive,
):
    session, uow, actor = await setup(ledger, tmp_path, monkeypatch)
    try:
        assert await session.scalar(text("PRAGMA foreign_keys")) == 1
        board = await session.get(Board, BOARD)
        board.settings = {**board.settings, "bug_learning_closeout": policy,
            "skip_cognitive_consolidation": skip_cognitive}
        await session.commit()
        command = request(contribution)
        if policy == "blocking" or contribution == "missing":
            expected = "bug_learning_capture_required" if policy == "blocking" else "delivery_evidence_incomplete"
            with pytest.raises(ValueError) as failure:
                await RecordCardDeliveryEvidenceUseCase().execute(command, actor=actor, uow=uow)
            assert expected in str(failure.value) or getattr(failure.value, "code", None) == expected
            await session.commit()
            assert (await session.get(Card, "task")).status.value == "in_progress"
            assert list(await session.scalars(select(CardDeliveryEvidenceRecordRow.id))) == []
            assert list(await session.scalars(select(DomainEventRow.id))) == []
        else:
            await RecordCardDeliveryEvidenceUseCase().execute(command, actor=actor, uow=uow)
            assert (await session.get(Card, "task")).status.value == "done"
        async with build_community_session_factory(session.bind)() as reader:
            card = await reader.get(Card, "task")
            completed = policy == "advisory" and contribution == "complete"
            assert card.status.value == ("done" if completed else "in_progress")
            assert len(card.conclusions or []) == (1 if completed else 0)
            assert not card.learning_closeout_bindings
            if completed:
                records = list(await reader.scalars(select(CardDeliveryEvidenceRecordRow)))
                assert len(records) == 1
                assert card.conclusions[0]["delivery_manifest"]["records"][0]["id"] == records[0].id
    finally:
        await session.close()


@pytest.mark.parametrize("damage", [None, "delivery", "permission", "evidence"])
async def test_blocking_capture_and_delivery_close_atomically_before_graph_materialization(
    ledger, tmp_path, monkeypatch, damage,
):
    from okto_pulse.community.adapters.bug_cognitive_context import CommunityBugCognitiveContextAssembler, CommunityCanonicalBugNodeReader
    from okto_pulse.community.adapters.sqlalchemy_kg_cognitive_source import CommunitySqlAlchemyCognitiveSourceStore
    from okto_pulse.core.ports.bug_cognitive_context import register_bug_cognitive_context_assembler
    from okto_pulse.core.ports.kg_cognitive_source import register_cognitive_source_store
    from okto_pulse.core.application.use_cases.learning_capture import LEARNING_CAPTURE_CREATE_PERMISSIONS
    from okto_pulse.core.domain.learning_submission import LearningSubmission
    from okto_pulse.core.events.bus import register_handler
    from okto_pulse.core.events.handlers.learning_capture import LearningCaptureMaterializationEnqueuer
    from okto_pulse.core.events.types import LearningCaptureAdmitted
    from test_adopted_delivery_report import contract

    session, uow, actor = await setup(ledger, tmp_path, monkeypatch)
    try:
        factory = build_community_session_factory(session.bind)
        store = CommunitySqlAlchemyCognitiveSourceStore(factory)
        assembler = CommunityBugCognitiveContextAssembler(CommunityCanonicalBugNodeReader())
        register_cognitive_source_store(store)
        register_bug_cognitive_context_assembler(assembler)
        register_handler(LearningCaptureAdmitted.event_type)(LearningCaptureMaterializationEnqueuer)
        board = await session.get(Board, BOARD)
        board.settings = {**board.settings, "bug_learning_closeout": "blocking", "skip_cognitive_consolidation": False}
        await session.commit()
        source = await assembler.assemble_semantic(session, board_id=BOARD, bug_id="task")
        assert source.verified, source.load_errors
        assert source.canonical_bug_present is None
        assert (await session.get(Card, "task")).status.value == "in_progress"
        assert await store.enumerate(BOARD) == ()
        submission = LearningSubmission(capture_id="native-closeout",
            expected_source_digest=source.source_digest, expected_source_version=source.source_policy_version,
            content="Generate version metadata from the release source.", context="Compiled About page",
            applicability="Frontend bundles containing release metadata", scenario_ids=[contract.signed.SCENARIO["id"]])
        if damage == "evidence":
            submission = submission.model_copy(update={"scenario_ids": ["unrelated"]})
        command = request("missing" if damage == "delivery" else "complete")
        command = command.model_copy(update={"report": command.report.model_copy(update={"learning_submission": submission})})
        actor = type(actor)(actor.actor_id, actor.source, actor_kind=actor.actor_kind,
            board_id=BOARD, permissions=[*actor.permissions, *LEARNING_CAPTURE_CREATE_PERMISSIONS])
        if damage == "permission":
            actor.permissions = [flag for flag in actor.permissions if flag != "kg.session.add_edge"]
        if damage:
            from okto_pulse.core.application.use_cases.base import PermissionDeniedError
            expected = {"delivery": "delivery_evidence_incomplete",
                "evidence": "learning_capture_evidence_not_authenticated",
                "permission": "permission_denied"}[damage]
            with pytest.raises((ValueError, PermissionDeniedError)) as failure:
                await RecordCardDeliveryEvidenceUseCase().execute(command, actor=actor, uow=uow)
            assert expected in str(failure.value) or getattr(failure.value, "code", None) == expected
            await session.commit()
            async with factory() as reader:
                card = await reader.get(Card, "task")
                assert card.status.value == "in_progress" and not card.conclusions and not card.learning_closeout_bindings
                assert list(await reader.scalars(select(CardDeliveryEvidenceRecordRow.id))) == []
                assert list(await reader.scalars(select(DomainEventRow.id))) == []
            assert await store.enumerate(BOARD) == ()
            return
        result = await RecordCardDeliveryEvidenceUseCase().execute(command, actor=actor, uow=uow)
        async with factory() as reader:
            card = await reader.get(Card, "task")
            assert card.status.value == "done"
            assert len(card.learning_closeout_bindings) == 1
            assert card.conclusions[-1]["delivery_manifest"]["records"][0]["id"] == result["entries"][0]["id"]
        records = await store.enumerate(BOARD)
        assert len(records) == 1 and records[0].payload["capture_format"] == "learning-capture/v2"
        assert records[0].payload["source"]["bug_id"] == "task"
    finally:
        await session.close()
