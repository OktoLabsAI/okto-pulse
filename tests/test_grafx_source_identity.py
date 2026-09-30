"""Native transactional lookup preserves scope and exposes ambiguity."""
# Imported pytest fixtures are injected again as test arguments.
# ruff: noqa: F811

import pytest
from okto_pulse.core.kg.interfaces.graph_errors import GraphError

from test_grafx_graph_transaction import BOARD_ID, _provider, fence, grafx_database  # noqa: F401


@pytest.mark.asyncio
async def test_source_identity_reads_staged_changes_and_excludes_superseded_and_other_types(
    grafx_database, fence,
):
    provider = _provider(grafx_database, fence)
    scope = await provider.begin(BOARD_ID)
    aliases = ('bug:id', 'card:id', 'card:bug:id')
    try:
        scope.create_node('Entity', 'historical',
            {'source_artifact_ref': aliases[0], 'superseded_by': 'current'},
            source_session_id='birth')
        scope.create_node('Entity', 'current',
            {'source_artifact_ref': aliases[2], 'superseded_by': ''},
            source_session_id='birth')
        scope.create_node('Decision', 'different-type',
            {'source_artifact_ref': aliases[1]}, source_session_id='birth')
        assert scope.find_active_node_ids_by_source_refs('Entity', aliases) == ('current',)
        assert scope.find_active_node_ids_by_source_refs('Entity', ('card:other',)) == ()
        scope.create_node('Entity', 'duplicate',
            {'source_artifact_ref': aliases[1]}, source_session_id='birth')
        assert scope.find_active_node_ids_by_source_refs('Entity', aliases) == ('current', 'duplicate')
        scope.create_node('Entity', 'third',
            {'source_artifact_ref': aliases[0]}, source_session_id='birth')
        assert len(scope.find_active_node_ids_by_source_refs('Entity', aliases)) == 2
    finally:
        await scope.rollback()


@pytest.mark.asyncio
async def test_source_identity_never_reads_another_board_or_a_closed_transaction(grafx_database, fence):
    provider = _provider(grafx_database, fence)
    with pytest.raises(GraphError, match='begin failed') as caught:
        await provider.begin('another-board')
    assert isinstance(caught.value.__cause__, KeyError)
    scope = await provider.begin(BOARD_ID)
    await scope.rollback()
    with pytest.raises(GraphError, match='already finished'):
        scope.find_active_node_ids_by_source_refs('Entity', ('card:id',))
