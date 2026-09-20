"""Sealed offline retirement, including commits lost before SQL acknowledgement."""

from dataclasses import replace
from datetime import datetime, timezone
import json

import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.community.adapters import retirement_offline_run as offline
from okto_pulse.community.adapters import retirement_materialization as materialization
from okto_pulse.community.adapters import retirement_materialization_plan as plans
from okto_pulse.community.adapters import sqlalchemy_database as db
from okto_pulse.community.adapters.global_outbox_retirement import _snapshot
from okto_pulse.community.adapters.graph_backend_binding import CommunityGraphBackendBindingStore
from okto_pulse.community.adapters.joint_recovery_snapshot import RecoveryGraph
from okto_pulse.community.adapters.sqlalchemy_models import ConsolidationAudit, GlobalUpdateOutbox, KuzuNodeRef
from test_grafx_global_retirement import corpora
from test_retirement_offline_run import inputs, MIGRATION
from test_card_validation_retirement import raw_cards
from test_graph_binding_publication_window import child as binding_probe
from test_schema_lifecycle_reentry import probe as schema_probe
from test_joint_recovery_window import _start as startup_probe
from test_card_context_retirement import dump
from logical_transfer_matrix_support import seed_generation, open_generation_database
import test_sprint_retirement_inventory as relational

database = relational.database


async def seed_outbox(engine):
    now = datetime(2026, 9, 20, tzinfo=timezone.utc)
    async with engine.begin() as connection:
        for identity, node_keys in (("exclusive", (("Entity", "root"), ("Criterion", "outcome"))),
                ("mixed", (("Entity", "root"), ("Criterion", "outcome"), ("Entity", "spec")))):
            counts = dict(nodes_added=len(node_keys), nodes_updated=0, nodes_superseded=0, edges_added=4)
            await connection.execute(insert(ConsolidationAudit).values(session_id=identity, board_id="board-a",
                artifact_type="sprint", artifact_id="sprint", agent_id="system:historical_consolidation",
                started_at=now, committed_at=now, undo_status="none", summary_text="original operational history", **counts))
            for index, (kind, key) in enumerate(node_keys):
                await connection.execute(insert(KuzuNodeRef).values(id=f"{identity}-{index}", board_id="board-a",
                    session_id=identity, kuzu_node_type=kind, kuzu_node_id=key, operation="add", timestamp=now))
            await connection.execute(insert(GlobalUpdateOutbox).values(id=identity, event_id=f"evt-{identity}",
                board_id="board-a", session_id=identity, event_type="consolidation_committed", created_at=now,
                payload={"session_id": identity, "artifact_id": "sprint", **counts}, retry_count=2, last_error="original error"))


async def setup(engine, tmp_path, *, global_present=True, other_board_graph=True):
    await seed_outbox(engine)
    args = await inputs(engine, tmp_path)
    bindings = CommunityGraphBackendBindingStore(args[4]["kg_base_dir"])
    board, global_corpus = corpora()
    board = replace(board, nodes=tuple(replace(node, properties={**node.properties, "source_artifact_ref": "sprint:sprint"})
        if node.properties.get("source_artifact_ref") == "sprint:origin" else node for node in board.nodes))
    surviving_board = replace(board, nodes=tuple(replace(node, properties={**node.properties,
        "source_artifact_ref": "spec:spec-b"}) for node in board.nodes))
    graphs = []
    for scope, owner, corpus in (("board", "board-a", board), ("board", "board-b", surviving_board),
            ("global_discovery", None, global_corpus)):
        if owner == "board-b" and not other_board_graph:
            continue
        if scope == "global_discovery" and not global_present:
            continue
        path = bindings.board_grafx_path(owner, "g1") if scope == "board" else bindings.global_grafx_path("g1")
        path.parent.mkdir(parents=True, exist_ok=True)
        seed_generation("grafx", path, corpus)
        native = open_generation_database("grafx", path, scope, read_only=False)
        options = dict(backend="grafx", generation="g1", physical_path=path, page_size=8192, database=native)
        if scope == "board":
            bindings.initialize_board_binding(board_id=owner, **options)
        else:
            bindings.initialize_global_binding(**options)
        graphs.append(RecoveryGraph(native, scope, owner))
    return args, graphs


async def prepare(args, graphs):
    return await offline.prepare_offline_retirement_run(args[0], args[1], tuple(graphs), args[2], args[3], **args[4])


async def resume(args, graphs, run, runtime=None):
    return await offline.resume_offline_retirement_materialization(runtime or args[0], args[1], tuple(graphs), run,
        migration_builds=MIGRATION)


@pytest.mark.asyncio
@pytest.mark.parametrize("global_present", [True, False])
async def test_sealed_original_graphs_outbox_and_all_three_fences(database, tmp_path, monkeypatch, global_present):
    engine, path = database
    args, graphs = await setup(engine, tmp_path, global_present=global_present)
    try:
        original_cards = await raw_cards(engine)
        run = await prepare(args, graphs)
        assert await raw_cards(engine) == original_cards
        document, *_ = offline.read_offline_retirement_run(run)
        retained = plans.decode_materialization_plan(document["materialization"])
        assert retained.selected_ids == ("exclusive",)
        assert document["format"] == "retirement-offline-run/v2"
        before = json.loads(retained.original)
        seen = []
        apply_board = materialization.apply_sprint_graph_retirement
        def checked(*a, **kw):
            assert binding_probe(args[4]["kg_base_dir"]) == "blocked"
            assert schema_probe(db._schema_process_lock_path(args[0])) == "blocked"
            assert all(startup_probe(root) == "blocked" for root in args[4]["runtime_directories"])
            seen.append(True)
            return apply_board(*a, **kw)
        monkeypatch.setattr(materialization, "apply_sprint_graph_retirement", checked)
        result = await resume(args, graphs, run)
        assert result["state"] == "materialization_retired" and seen == [True, True]
        assert (await raw_cards(engine))["c1"]["sprint_id"] is None
        async with engine.connect() as connection:
            after = json.loads(await connection.run_sync(_snapshot))
            records = (await connection.exec_driver_sql("SELECT ordinal FROM retirement_data_checkpoints ORDER BY ordinal")).scalars().all()
        assert records == list(range(6))
        for table in ("consolidation_audit", "kuzu_node_refs"):
            assert before[table] == after[table]
        columns = before["global_update_outbox"]["columns"]
        for a, b in zip(before["global_update_outbox"]["rows"], after["global_update_outbox"]["rows"], strict=True):
            if a[columns.index("id")][1] == "exclusive":
                a[columns.index("retry_count")] = ["integer", "-2"]
            assert a == b
        lsns, sql = tuple(graph.database.transactions.published_lsn() for graph in graphs), dump(path)
        assert await resume(args, graphs, run) == result
        assert tuple(graph.database.transactions.published_lsn() for graph in graphs) == lsns
        assert dump(path) == sql
        with pytest.raises(Exception, match="retirement_cutover_incomplete"):
            await offline.require_retirement_runtime_admission(engine)
        if not global_present:
            assert not (args[4]["kg_base_dir"] / "global").exists()
    finally:
        for graph in graphs:
            graph.database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["board", "global", "sql"])
async def test_cold_resume_after_committed_graph_or_sql_without_recapture(database, tmp_path, monkeypatch, stage):
    engine, path = database
    args, graphs = await setup(engine, tmp_path)
    try:
        run = await prepare(args, graphs)
        if stage == "sql":
            original = offline.resume_retirement_materialization
            async def lost(*a, **kw):
                await original(*a, **kw)
                raise RuntimeError("lost after commit")
            module, name, wrapper = offline, "resume_retirement_materialization", lost
        else:
            name = "apply_sprint_graph_retirement" if stage == "board" else "apply_global_graph_retirement"
            original = getattr(materialization, name)
            def lost_graph(*a, **kw):
                original(*a, **kw)
                raise RuntimeError("lost after commit")
            module, wrapper = materialization, lost_graph
        with monkeypatch.context() as scoped:
            scoped.setattr(module, name, wrapper)
            with pytest.raises(RuntimeError, match="lost after commit"):
                await resume(args, graphs, run)
        with pytest.raises(Exception, match="retirement_cutover_incomplete"):
            await offline.require_retirement_runtime_admission(engine)
        def forbidden(*a, **kw):
            raise AssertionError("must use sealed originals")
        monkeypatch.setattr(offline, "prepare_materialization_plan", forbidden)
        monkeypatch.setattr(offline, "capture_permission_retirement_checkpoint", forbidden)
        monkeypatch.setattr(offline, "capture_sprint_retirement_archive", forbidden)
        for index, graph in enumerate(graphs):
            native_path = graph.database.path
            graph.database.close()
            graphs[index] = replace(graph, database=open_generation_database("grafx", native_path, graph.scope, read_only=False))
        await engine.dispose()
        reopened = create_async_engine(f"sqlite+aiosqlite:///{path}")
        try:
            runtime = db.CommunityDatabaseRuntime(reopened, async_sessionmaker(reopened))
            result = await resume(args, graphs, run, runtime)
            assert result["state"] == "materialization_retired"
            assert await resume(args, graphs, run, runtime) == result
        finally:
            await reopened.dispose()
    finally:
        for graph in graphs:
            graph.database.close()


@pytest.mark.asyncio
async def test_completion_trigger_cannot_drop_authority_or_commit_partial_outbox(database, tmp_path):
    engine, _ = database
    args, graphs = await setup(engine, tmp_path)
    try:
        run = await prepare(args, graphs)
        async with engine.begin() as connection:
            await connection.exec_driver_sql("CREATE TRIGGER damage_graph_ack AFTER INSERT ON retirement_data_checkpoints "
                "WHEN NEW.ordinal=5 BEGIN DELETE FROM permission_introduction_audit WHERE phase='permission_retirement_capture'; END")
        with pytest.raises(ValueError, match="permission_retirement"):
            await resume(args, graphs, run)
        async with engine.begin() as connection:
            assert (await connection.execute(text("SELECT retry_count FROM global_update_outbox WHERE id='exclusive'"))).scalar_one() == 2
            assert (await connection.exec_driver_sql("SELECT max(ordinal) FROM retirement_data_checkpoints")).scalar_one() == 4
            assert (await connection.exec_driver_sql("SELECT count(*) FROM permission_introduction_audit WHERE phase='permission_retirement_capture'")).scalar_one() > 0
            await connection.exec_driver_sql("DROP TRIGGER damage_graph_ack")
        assert (await resume(args, graphs, run))["state"] == "materialization_retired"
    finally:
        for graph in graphs:
            graph.database.close()


@pytest.mark.asyncio
async def test_missing_intent_after_graph_commit_is_not_recreated(database, tmp_path, monkeypatch):
    engine, path = database
    args, graphs = await setup(engine, tmp_path)
    try:
        run = await prepare(args, graphs)
        original = materialization.apply_sprint_graph_retirement
        def lost(*a, **kw):
            original(*a, **kw)
            raise RuntimeError("lost")
        with monkeypatch.context() as scoped:
            scoped.setattr(materialization, "apply_sprint_graph_retirement", lost)
            with pytest.raises(RuntimeError, match="lost"):
                await resume(args, graphs, run)
        async with engine.begin() as connection:
            await connection.exec_driver_sql("DROP TRIGGER retirement_data_no_delete")
            await connection.exec_driver_sql("DELETE FROM retirement_data_checkpoints WHERE ordinal=4")
        before = dump(path)
        with pytest.raises(ValueError, match="graph_state_mismatch"):
            await resume(args, graphs, run)
        assert dump(path) == before
    finally:
        for graph in graphs:
            graph.database.close()


@pytest.mark.asyncio
async def test_entirely_absent_materialization_is_preserved_as_absent(database, tmp_path):
    engine, _ = database
    args = await inputs(engine, tmp_path)
    run = await prepare(args, ())
    assert (await resume(args, (), run))["state"] == "materialization_retired"
    assert not (args[4]["kg_base_dir"] / "boards").exists()
    assert not (args[4]["kg_base_dir"] / "global").exists()


@pytest.mark.asyncio
async def test_cached_digest_without_its_board_source_requires_review_before_data_transform(database, tmp_path):
    engine, _ = database
    args, graphs = await setup(engine, tmp_path, other_board_graph=False)
    try:
        before = await raw_cards(engine)
        with pytest.raises(ValueError, match="missing_board_source_requires_review"):
            await prepare(args, graphs)
        assert await raw_cards(engine) == before
        assert not args[3].exists()
    finally:
        for graph in graphs:
            graph.database.close()
