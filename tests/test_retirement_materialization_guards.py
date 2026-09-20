"""Retained SQL receipts cannot replace current graph and routing evidence."""

from dataclasses import replace

import pytest

from okto_pulse.community.adapters import retirement_offline_run as offline
from okto_pulse.community.adapters.retirement_data_journal import record_retirement_stage
from okto_pulse.community.adapters.retirement_materialization import MaterializationCheckpoint
from okto_pulse.community.adapters.global_outbox_retirement import _sha
from okto_pulse.community.adapters.sprint_retirement_archive import _encode
from test_retirement_offline_materialization import setup, prepare, resume
from test_retirement_offline_run import MIGRATION
from test_card_validation_retirement import raw_cards
from test_card_context_retirement import dump
import test_sprint_retirement_inventory as relational

database = relational.database


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["wrong_board_handle", "changed_graph"])
async def test_graph_binding_and_before_state_precede_any_card_transformation(database, tmp_path, damage):
    engine, path = database
    args, graphs = await setup(engine, tmp_path)
    try:
        run = await prepare(args, graphs)
        selected = list(graphs)
        if damage == "wrong_board_handle":
            selected[1] = replace(selected[1], database=graphs[0].database)
            expected = "selection_mismatch"
        else:
            transaction = graphs[0].database.begin("write")
            transaction.execute("MATCH (n:Entity) WHERE n.id=$identity DETACH DELETE n", {"identity": "spec"})
            transaction.commit()
            expected = "graph_state_mismatch"
        before = dump(path)
        with pytest.raises(ValueError, match=expected):
            await resume(args, selected, run)
        assert dump(path) == before
        assert (await raw_cards(engine))["c1"]["sprint_id"] == "sprint"
    finally:
        for graph in graphs:
            graph.database.close()


@pytest.mark.asyncio
async def test_coherent_fabricated_sql_completion_does_not_certify_unchanged_graphs(database, tmp_path):
    engine, path = database
    args, graphs = await setup(engine, tmp_path)
    try:
        run = await prepare(args, graphs)
        document, _, _, data, backup, _ = offline.read_offline_retirement_run(run)
        await offline.resume_offline_retirement_data(args[0], args[1], run, migration_builds=MIGRATION)
        value = document["materialization"]
        fabricated = MaterializationCheckpoint(data.migration_id, _sha(_encode(value)), backup.manifest_sha256, value["after_sha256"])
        # Deliberately bypass the transaction owner: a syntactically valid
        # hash chain in candidate SQL is still not a graph completion proof.
        async with engine.connect() as connection:
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            await record_retirement_stage(connection, data, "graph_intent", fabricated, replay=False)
            await record_retirement_stage(connection, data, "graphs", fabricated, replay=False)
            await connection.commit()
        before = dump(path)
        with pytest.raises(ValueError, match="graph_state_mismatch"):
            await resume(args, graphs, run)
        assert dump(path) == before
        with pytest.raises(Exception, match="retirement_cutover_incomplete"):
            await offline.require_retirement_runtime_admission(engine)
    finally:
        for graph in graphs:
            graph.database.close()
