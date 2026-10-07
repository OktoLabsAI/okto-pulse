"""Exact capture/target revision selection across an audited SQL history."""
from dataclasses import replace

import pytest
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from okto_pulse.community.adapters.sqlalchemy_models import KGCognitiveSourceRevision
from okto_pulse.core.ports.kg_cognitive_source import (
    CognitiveSourceConflict, FingerprintCognitiveSourceReader,
)
from test_kg_cognitive_source_adapter import BOARD, _record, store as _store

store = _store
pytestmark = pytest.mark.asyncio


async def test_exact_fingerprint_selects_historical_revision_without_changing_head(store):
    adapter, factory = store
    assert isinstance(adapter, FingerprintCognitiveSourceReader)
    records = tuple(_record('learning-selection', title=title) for title in ('birth', 'capture', 'literal'))
    for record in records:
        await adapter.append(record)
    original = await adapter.enumerate(BOARD)
    async with factory() as session:
        for expected in original:
            actual = await adapter.read_fingerprint_in_context(session, board_id=BOARD,
                node_id=expected.node_id, generation=0, fingerprint=expected.record_fingerprint)
            assert actual == expected
        assert await adapter.read_fingerprint_in_context(session, board_id=BOARD,
            node_id=records[0].node_id, generation=0, fingerprint='a' * 64) is None
        assert await adapter.read_fingerprint_in_context(session, board_id=BOARD,
            node_id='missing', generation=0, fingerprint=records[0].record_fingerprint) is None
        with pytest.raises(CognitiveSourceConflict, match='scope_conflict'):
            await adapter.read_fingerprint_in_context(session, board_id='other-board',
                node_id=records[0].node_id, generation=0, fingerprint=records[0].record_fingerprint)
    assert await adapter.enumerate(BOARD) == original


@pytest.mark.parametrize('field,value', [('board_id', ''), ('node_id', True), ('generation', True),
    ('generation', -1), ('fingerprint', 'A' * 64), ('fingerprint', 'a' * 63), ('fingerprint', None)])
async def test_fingerprint_selector_refuses_invalid_identity(store, field, value):
    adapter, factory = store
    args = dict(board_id=BOARD, node_id='learning', generation=0, fingerprint='a' * 64)
    args[field] = value
    async with factory() as session:
        with pytest.raises(ValueError, match='fingerprint_selection_invalid'):
            await adapter.read_fingerprint_in_context(session, **args)


async def test_selecting_birth_does_not_hide_corrupt_later_revision(store, monkeypatch):
    adapter, factory = store
    first = _record('learning-corruption')
    await adapter.append(first)
    changed = replace(first, payload={**first.payload, 'content': 'new'}, record_fingerprint='')
    revision_id = await adapter.append(changed)
    async with factory() as session:
        with pytest.raises(IntegrityError, match='kg_cognitive_source_immutable'):
            await session.execute(update(KGCognitiveSourceRevision).where(
                KGCognitiveSourceRevision.id == revision_id).values(payload={'content': 'tampered'}))
        await session.rollback()
    # Simulate a corrupt read independently of the enforced write protection.
    from okto_pulse.community.adapters import sqlalchemy_kg_cognitive_source as module
    original = module._load_revision_rows
    async def corrupt(*args, **kwargs):
        rows = await original(*args, **kwargs)
        next(row for row in rows if row.id == revision_id).payload = {'content': 'tampered'}
        return rows
    monkeypatch.setattr(module, '_load_revision_rows', corrupt)
    async with factory() as session:
        with pytest.raises(CognitiveSourceConflict, match='fingerprint_mismatch'):
            await adapter.read_fingerprint_in_context(session, board_id=BOARD,
                node_id=first.node_id, generation=0, fingerprint=first.record_fingerprint)
