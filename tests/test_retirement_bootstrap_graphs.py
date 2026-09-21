"""Bootstrap checkpoint keeps native Grafx and outbox retirement sealed."""

import pytest

from okto_pulse.community.adapters import retirement_offline_run as offline
from okto_pulse.community.adapters.relational_schema_lifecycle import CommunityRelationalSchemaLifecycleOrchestrator
import test_retirement_bootstrap_convergence as convergence
from test_retirement_offline_materialization import prepare, setup
from test_retirement_offline_schema import schema
from test_retirement_offline_run import MIGRATION
from test_card_context_retirement import dump

database = convergence.database


@pytest.mark.asyncio
@pytest.mark.parametrize('global_present', [True, False])
async def test_bootstrap_and_replay_preserve_populated_grafx(database, tmp_path, monkeypatch, global_present):
    engine, path = database
    args, graphs = await setup(engine, tmp_path, global_present=global_present)
    try:
        run = await prepare(args, graphs)
        await schema(args, graphs, run)
        lsns = tuple(graph.database.transactions.published_lsn() for graph in graphs)
        async def complete():
            return await offline.resume_offline_retirement_bootstrap(args[0], args[1], tuple(graphs), run,
                migration_builds=MIGRATION)
        result = await complete()
        assert result['state'] == 'bootstrap_complete'
        assert tuple(graph.database.transactions.published_lsn() for graph in graphs) == lsns
        before = dump(path)
        async def forbidden(*args):
            pytest.fail('bootstrap writers repeated during replay')
        monkeypatch.setattr(CommunityRelationalSchemaLifecycleOrchestrator, 'initialize_schema', forbidden)
        assert await complete() == result
        assert dump(path) == before
        assert tuple(graph.database.transactions.published_lsn() for graph in graphs) == lsns
        with pytest.raises(Exception, match='retirement_cutover_incomplete'):
            await offline.require_retirement_runtime_admission(engine)
    finally:
        for graph in graphs:
            graph.database.close()
