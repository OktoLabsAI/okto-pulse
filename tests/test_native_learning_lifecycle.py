"""One native Bug: durable capture, actual review gates, then projection."""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from okto_pulse.community.adapters.sqlalchemy_database import build_community_session_factory
from okto_pulse.community.adapters.sqlalchemy_models import Board, Card, DomainEventRow, ConsolidationQueue
from okto_pulse.community.adapters.bug_cognitive_context import CommunityBugCognitiveContextAssembler, CommunityCanonicalBugNodeReader
from okto_pulse.community.adapters.sqlalchemy_kg_cognitive_source import CommunitySqlAlchemyCognitiveSourceStore
from okto_pulse.community.adapters.sqlalchemy_amendment_revision import CommunitySqlAlchemyAmendmentRevisionStore
from okto_pulse.core.ports.amendment_revision import register_amendment_revision_store
from okto_pulse.core.application.use_cases.base import ActorContext, PermissionDeniedError
from okto_pulse.core.application.use_cases.card_crud import SubmitTaskValidationCommand, SubmitTaskValidationUseCase
from okto_pulse.core.application.use_cases.delivery_evidence import RecordCardDeliveryEvidenceUseCase
from okto_pulse.core.application.use_cases.learning_capture import LEARNING_CAPTURE_CREATE_PERMISSIONS, LEARNING_CAPTURE_HISTORY_PERMISSIONS
from okto_pulse.core.domain.learning_submission import LearningSubmission
from okto_pulse.core.models.schemas import CardStatus
from okto_pulse.core.domain.learning_closeout import closeout_binding_is_current
from okto_pulse.core.ports.bug_cognitive_context import register_bug_cognitive_context_assembler, register_canonical_bug_node_read_port
from okto_pulse.core.ports.kg_cognitive_source import register_cognitive_source_store
from okto_pulse.core.events.bus import register_handler
from okto_pulse.core.events.handlers.learning_capture import LearningCaptureMaterializationEnqueuer
from okto_pulse.core.events.types import LearningCaptureAdmitted
from test_adopted_delivery_report import ledger as _ledger, setup, request, BOARD, SPEC, contract

ledger = _ledger
pytestmark = [pytest.mark.asyncio, pytest.mark.parametrize("ledger", ["native_bug_lifecycle"], indirect=True)]


def configure_graph(factory, tmp_path):
    from okto_grafx import connect
    from okto_pulse.core import configure_settings
    from okto_pulse.core.ports.coordination import register_coordination_providers
    from okto_pulse.core.ports.consolidation import register_consolidation_persistence_port
    from okto_pulse.core.ports.canonical_debt import register_canonical_debt_store
    from okto_pulse.community.config import CommunitySettings
    from okto_pulse.community.adapters.composition import configure_community_kg_registry, require_community_routed_graph_composition
    from okto_pulse.community.adapters.coordination import register_community_coordination_providers, build_root_bound_community_write_lock_port
    from okto_pulse.community.adapters.graph_backend_binding import CommunityGraphBackendBindingStore
    from okto_pulse.community.adapters.sqlalchemy_consolidation import CommunitySqlAlchemyConsolidationPersistence
    from okto_pulse.community.adapters.sqlalchemy_canonical_debt import CommunitySqlAlchemyCanonicalDebtStore
    from logical_transfer_matrix_support import one_node_corpus, seed_generation

    settings = CommunitySettings(data_dir=str(tmp_path / "runtime"), kg_base_dir=str(tmp_path / "kg"),
        kg_embedding_mode="stub", kg_embedding_dim=384)
    configure_settings(settings)
    register_community_coordination_providers()
    register_coordination_providers(write_lock_port=build_root_bound_community_write_lock_port(tmp_path / "kg"))
    register_consolidation_persistence_port(CommunitySqlAlchemyConsolidationPersistence())
    register_canonical_debt_store(CommunitySqlAlchemyCanonicalDebtStore())
    from okto_pulse.core.ports.kg_health import register_kg_health_read_port
    from okto_pulse.community.adapters.sqlalchemy_kg_health import CommunitySqlAlchemyKGHealthReader
    register_kg_health_read_port(CommunitySqlAlchemyKGHealthReader())
    from okto_pulse.core.ports.queue_health import register_queue_health_read_port
    from okto_pulse.community.adapters.sqlalchemy_queue_health import CommunitySqlAlchemyQueueHealthReader
    register_queue_health_read_port(CommunitySqlAlchemyQueueHealthReader())
    bindings = CommunityGraphBackendBindingStore(tmp_path / "kg")
    physical = bindings.board_grafx_path(BOARD, "native-lifecycle")
    physical.parent.mkdir(parents=True, exist_ok=True)
    seed_generation("grafx", physical, replace(one_node_corpus("board", key="unused"), nodes=(), relations=()))
    with connect(physical, page_size=8192) as graph:
        bindings.initialize_board_binding(board_id=BOARD, backend="grafx", generation="native-lifecycle",
            physical_path=physical, page_size=8192, database=graph)
    configure_community_kg_registry(factory, settings=settings)
    return require_community_routed_graph_composition()


@pytest.mark.parametrize("same_reviewer", [False, True])
async def test_capture_is_durable_before_actual_review_and_materializes_after_done(
    ledger, tmp_path, monkeypatch, same_reviewer
):
    # A native authored workflow must not activate an optional LLM bridge or
    # contact a provider, even if a caller catches and suppresses its failure.
    # Keep an attempt ledger as well as refusing the operation.
    import socket
    from contextvars import ContextVar
    from okto_pulse.core.kg.llm_provider_bridge_cache import BridgeCacheRegistry

    activation_attempts = []

    def refuse_bridge(*args, **kwargs):
        activation_attempts.append("llm_bridge")
        raise AssertionError("native_learning_must_not_activate_llm_bridge")

    def refuse_network(*args, **kwargs):
        activation_attempts.append("outbound_connection")
        raise AssertionError("native_learning_must_not_contact_provider")

    # Windows asyncio uses stdlib socketpair's private loopback connection
    # for its wakeup pipe. Permit only the original socketpair constructor,
    # not arbitrary loopback connections (a local LLM could use those).
    constructing_socketpair = ContextVar("native_test_socketpair", default=False)
    original_socketpair = socket.socketpair
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def socketpair(*args, **kwargs):
        token = constructing_socketpair.set(True)
        try:
            return original_socketpair(*args, **kwargs)
        finally:
            constructing_socketpair.reset(token)

    def connect(sock, address):
        if constructing_socketpair.get():
            return original_connect(sock, address)
        return refuse_network(sock, address)

    def connect_ex(sock, address):
        if constructing_socketpair.get():
            return original_connect_ex(sock, address)
        return refuse_network(sock, address)

    monkeypatch.setattr(BridgeCacheRegistry, "get_or_create", refuse_bridge)
    monkeypatch.setattr(socket, "socketpair", socketpair)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "create_connection", refuse_network)
    session, uow, owner = await setup(ledger, tmp_path, monkeypatch)
    bundle = None
    try:
        factory = build_community_session_factory(session.bind)
        bundle = configure_graph(factory, tmp_path)
        register_amendment_revision_store(CommunitySqlAlchemyAmendmentRevisionStore())
        store = CommunitySqlAlchemyCognitiveSourceStore(factory)
        assembler = CommunityBugCognitiveContextAssembler(CommunityCanonicalBugNodeReader())
        register_cognitive_source_store(store)
        register_bug_cognitive_context_assembler(assembler)
        register_canonical_bug_node_read_port(CommunityCanonicalBugNodeReader())
        register_handler(LearningCaptureAdmitted.event_type)(LearningCaptureMaterializationEnqueuer)
        uow.boards = SimpleNamespace(get=uow.services.boards.get_board)
        board = await session.get(Board, BOARD)
        board.settings = {**board.settings, "bug_learning_closeout": "blocking",
            "skip_cognitive_consolidation": False, "require_task_validation": True}
        if same_reviewer:
            board.settings = {**board.settings, "reviewer_separation_mode": "off"}
        await session.commit()
        source = await assembler.assemble_semantic(session, board_id=BOARD, bug_id="task")
        assert source.verified, source.load_errors
        submission = LearningSubmission(capture_id="native-lifecycle",
            expected_source_digest=source.source_digest, expected_source_version=source.source_policy_version,
            content="Generate version metadata from the release source.", context="Compiled About page",
            applicability="Frontend bundles containing release metadata", scenario_ids=[contract.signed.SCENARIO["id"]])
        command = request("complete")
        command = command.model_copy(update={"report": command.report.model_copy(update={
            "status": CardStatus.VALIDATION, "learning_submission": submission})})
        executor = ActorContext(owner.actor_id, "mcp", actor_kind="agent", board_id=BOARD,
            permissions=[*owner.permissions, *LEARNING_CAPTURE_CREATE_PERMISSIONS, "card.move.in_progress_to_validation"])
        await RecordCardDeliveryEvidenceUseCase().execute(command, actor=executor, uow=uow)
        capture, = await store.enumerate(BOARD)
        assert CommunityCanonicalBugNodeReader().resolve_current(board_id=BOARD, bug_id="task") is None
        assert capture.payload["capture_format"] == "learning-capture/v2"
        async with factory() as reader:
            card = await reader.get(Card, "task")
            assert card.status.value == "validation" and not card.learning_closeout_bindings
            assert len(card.conclusions) == 1 and not card.validations
            source = await assembler.assemble_semantic(reader, board_id=BOARD, bug_id="task")
        from okto_pulse.core.kg.rebuild_audit import CognitiveConsolidationItemStore, require_rebuild_audit_artifact_store
        from okto_pulse.core.kg.workers.cognitive_closeout import CognitiveCloseoutWorker
        from test_learning_materialization_worker import deliver_capture_events
        work = CognitiveConsolidationItemStore(artifact_store=require_rebuild_audit_artifact_store())
        await deliver_capture_events(factory)
        worker = CognitiveCloseoutWorker(factory)
        assert await worker.drain_once() == 1
        item, = work.list_items(BOARD, work.latest_generation(BOARD))
        assert item.status == "pending" and item.reason == "learning_capture_awaiting_done"
        assert await store.enumerate(BOARD) == (capture,)
        review = dict(expected_subject_version=source.source_policy_version, idempotency_key="native-review",
            confidence=99, confidence_justification="Reviewed the actual implementation.",
            estimated_completeness=100, completeness_justification="All assigned work was checked.",
            estimated_drift=0, drift_justification="Implementation stayed within scope.",
            general_justification="Independent review of the correction.", recommendation="approve",
            learning_capture=dict(learning_id=capture.node_id, generation=capture.generation,
                fingerprint=capture.record_fingerprint))
        with pytest.raises(PermissionDeniedError):
            await SubmitTaskValidationUseCase().execute(SubmitTaskValidationCommand("task", review),
                actor=executor, uow=uow)
        await session.commit()
        async with factory() as reader:
            denied = await reader.get(Card, "task")
            assert denied.status.value == "validation" and not denied.validations
            assert not denied.learning_closeout_bindings
        assert await store.enumerate(BOARD) == (capture,)
        reviewer_id = executor.actor_id if same_reviewer else "independent-reviewer"
        reviewer = ActorContext(reviewer_id, "mcp", actor_kind="agent", board_id=BOARD,
            permissions=[*LEARNING_CAPTURE_HISTORY_PERMISSIONS, "card.validation.submit"])
        result = await SubmitTaskValidationUseCase().execute(SubmitTaskValidationCommand("task", review),
            actor=reviewer, uow=uow)
        assert result.validation["completion_outcome"] == "completed", json.dumps(result.validation, default=str)
        async with factory() as reader:
            card = await reader.get(Card, "task")
            assert card.status.value == "done" and len(card.learning_closeout_bindings) == 1
            assert card.validations[-1]["reviewer_id"] == reviewer_id
            closed = await assembler.assemble_semantic(reader, board_id=BOARD, bug_id="task")
            assert closeout_binding_is_current(card.learning_closeout_bindings[0], closed)
            events = list(await reader.scalars(select(DomainEventRow)))
            assert any(event.event_type == LearningCaptureAdmitted.event_type for event in events)
        assert await store.enumerate(BOARD) == (capture,)
        assert await worker.drain_once() == 1
        item, = work.list_items(BOARD, work.latest_generation(BOARD))
        assert item.status == "pending" and item.reason == "learning_capture_awaiting_canonical_bug"
        from okto_pulse.core.application.processors.consolidation import ConsolidationProcessor
        processor = ConsolidationProcessor(relational_scope_factory=factory)
        for kind, identity in (("spec", SPEC), ("card", "task")):
            async with factory() as writer:
                writer.add(ConsolidationQueue(board_id=BOARD, artifact_type=kind,
                    artifact_id=identity, source="state_transition"))
                await writer.commit()
            assert await processor.process_batch() == 1
        canonical = CommunityCanonicalBugNodeReader().resolve_current(board_id=BOARD, bug_id="task")
        assert canonical
        assert await worker.drain_once() == 1
        item, = work.list_items(BOARD, work.latest_generation(BOARD))
        assert item.status == "consolidated", item
        from okto_pulse.core.services.application_kg import get_current_provider_registry
        result = get_current_provider_registry().cypher_executor.execute_read_only(BOARD,
            "MATCH (n:Learning)-[:validates]->(b:Bug) RETURN n.id, b.id", {}, max_rows=10)
        assert not result.get("truncated")
        assert result["rows"] == [[capture.node_id, canonical]]
        history = await store.enumerate(BOARD)
        assert len(history) == 2 and history[0] == capture
        await deliver_capture_events(factory)
        assert await worker.drain_once() == 0
        assert await store.enumerate(BOARD) == history
        from okto_pulse.core.services.kg_health_service import get_kg_health
        async with factory() as reader:
            board = await reader.get(Board, BOARD)
            assert not board.settings.get("cognitive_llm_config")
            health = await get_kg_health(BOARD, reader)
        # This lifecycle fixture has no global-discovery binding or registered
        # relational source path. Health must report those limits; successful
        # local Learning projection does not establish whole-runtime health.
        assert health["probe_reason_codes"]["global_discovery"] == "graph_route_binding_missing"
        assert health["probe_diagnostics"]["rebuild_source_diagnostics"]["status"] == "unavailable"
        assert health["health_issues"]
        assert all("llm" not in json.dumps(issue).lower() for issue in health["health_issues"])
    finally:
        from okto_pulse.core.services.application_kg import drain_kg_health_probes
        drain_kg_health_probes()
        if bundle is not None:
            bundle.global_graph.close_all_on_shutdown()
            bundle.grafx_pool.close_all()
            for pool in (*bundle.board.grafx_read_pools, *bundle.board.grafx_query_pools):
                pool.close_all()
        await session.close()
    assert activation_attempts == []
