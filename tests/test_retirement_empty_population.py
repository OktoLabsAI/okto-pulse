"""A complete empty Sprint census needs no fabricated archive or entity."""

import pytest
from sqlalchemy import text

from okto_pulse.core.ports.context_disposition import ContextDispositionPlan
from okto_pulse.community.adapters.retirement_data_journal import (
    prepare_retirement_data_run, resume_retirement_data_run,
)
from okto_pulse.community.adapters.sprint_retirement_archive import capture_sprint_retirement_archive
from okto_pulse.community.adapters.storage import CommunityFileSystemStorage
from test_card_context_retirement import dump
import test_sprint_retirement_inventory as relational

database = relational.database


@pytest.mark.asyncio
async def test_empty_sprint_population_preserves_data_and_resumes_from_checkpoint(database, tmp_path):
    engine, path = database
    # Remove only the disposable fixture's sole legacy Sprint, before capture.
    async with engine.begin() as connection:
        await connection.execute(text('DELETE FROM sprints'))
    storage = CommunityFileSystemStorage(str(tmp_path / 'storage'))
    references = await capture_sprint_retirement_archive(engine, storage, migration_id='empty-cutover')
    assert references == ()
    plan = ContextDispositionPlan(migration_id='empty-cutover', decision_reference='empty-census', decisions=())
    run = await prepare_retirement_data_run(engine, storage, references, plan=plan)
    try:
        result = await resume_retirement_data_run(engine, storage, run, plan=plan)
    except ValueError as error:
        from dataclasses import asdict
        import json
        trace = error.__traceback__
        while trace is not None:
            if trace.tb_frame.f_code.co_name == 'install_empty_context':
                values = trace.tb_frame.f_locals
                if 'after' in values:
                    (tmp_path / 'empty-census-diagnostic.json').write_text(json.dumps({
                        key: asdict(values[key]) for key in ('before', 'after')}, default=str, indent=2))
            trace = trace.tb_next
        raise
    assert result['state'] == 'data_preserved'
    assert result['context'].candidate_count == result['context'].binding_count == 0
    assert result['cards'].card_count == result['cards'].override_count == 0
    before = dump(path)
    assert await resume_retirement_data_run(engine, storage, run, plan=plan) == result
    assert dump(path) == before
    async with engine.connect() as connection:
        assert await connection.scalar(text('SELECT COUNT(*) FROM sprints')) == 0
        assert await connection.scalar(text('SELECT COUNT(*) FROM specs')) == 2


@pytest.mark.asyncio
async def test_empty_references_cannot_hide_existing_sprint_population(database, tmp_path):
    engine, path = database
    storage = CommunityFileSystemStorage(str(tmp_path / 'storage'))
    plan = ContextDispositionPlan(migration_id='empty-cutover', decision_reference='empty-census', decisions=())
    before = dump(path)
    with pytest.raises(ValueError, match='context_disposition_archive_changed'):
        await prepare_retirement_data_run(engine, storage, (), plan=plan)
    assert dump(path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['context', 'cards', 'work'])
async def test_empty_population_resumes_each_committed_step_without_fabricating_events(database, tmp_path, monkeypatch, stage):
    from okto_pulse.community.adapters import context_disposition_retirement as context
    from okto_pulse.community.adapters import card_validation_retirement as cards
    from okto_pulse.community.adapters import sprint_work_retirement as work
    engine, path = database
    async with engine.begin() as connection:
        await connection.execute(text('DELETE FROM sprints'))
    storage = CommunityFileSystemStorage(str(tmp_path / 'storage'))
    plan = ContextDispositionPlan(migration_id='empty-cutover', decision_reference='empty-census', decisions=())
    run = await prepare_retirement_data_run(engine, storage, (), plan=plan)
    module, name = {'context': (context, 'install_context_dispositions'),
        'cards': (cards, 'materialize_archived_card_policies'), 'work': (work, 'supersede_archived_sprint_work')}[stage]
    original = getattr(module, name)
    async def interrupted(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError('lost empty-step response')
    with monkeypatch.context() as scoped:
        scoped.setattr(module, name, interrupted)
        with pytest.raises(RuntimeError, match='lost empty-step response'):
            await resume_retirement_data_run(engine, storage, run, plan=plan)
    result = await resume_retirement_data_run(engine, storage, run, plan=plan)
    before = dump(path)
    assert await resume_retirement_data_run(engine, storage, run, plan=plan) == result
    assert dump(path) == before
    async with engine.connect() as connection:
        assert await connection.scalar(text('SELECT COUNT(*) FROM domain_events')) == 0


@pytest.mark.asyncio
async def test_empty_context_requires_retained_checkpoint_and_rejects_wrong_receipt(database, tmp_path):
    from dataclasses import replace
    from okto_pulse.community.adapters import context_disposition_retirement as context
    engine, _ = database
    async with engine.begin() as connection:
        await connection.execute(text('DELETE FROM sprints'))
    storage = CommunityFileSystemStorage(str(tmp_path / 'storage'))
    plan = ContextDispositionPlan(migration_id='empty-cutover', decision_reference='empty-census', decisions=())
    with pytest.raises(ValueError, match='empty_checkpoint_required'):
        await context.install_context_dispositions(engine, storage, (), plan=plan)
    run = await prepare_retirement_data_run(engine, storage, (), plan=plan)
    result = await resume_retirement_data_run(engine, storage, run, plan=plan)
    with pytest.raises(ValueError, match='replay_mismatch'):
        await context.install_context_dispositions(engine, storage, (), plan=plan,
            expected_receipt=replace(result['context'], evidence_sha256='0' * 64), checkpoint_run=run)
