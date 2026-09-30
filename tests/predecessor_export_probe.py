"""Export genuine v0.3.4 Sprint history in a disposable predecessor process."""
import asyncio
import json
from pathlib import Path
import sys

from retirement_predecessor_probe import provenance


async def main():
    proof = provenance()
    database, output = map(Path, sys.argv[1:])
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from okto_pulse.community.adapters.sqlalchemy_models import Sprint, SprintQAItem, SprintHistory
    from okto_pulse.community.adapters.sqlalchemy_entity_export import CommunitySqlAlchemyEntityExportReader
    from okto_pulse.core.domain.entity_export import EntityExportRequest, EntityExportType, EntityExportDisclosure
    from okto_pulse.core.domain.realm import RealmScope

    engine = create_async_engine(f'sqlite+aiosqlite:///{database}')
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            sprint = await session.get(Sprint, 'sprint-a')
            sprint.evaluations = [{'id': 'export-evaluation', 'overall_justification': 'BASELINE-EVIDENCE-PRESERVED'}]
            session.add(SprintQAItem(id='export-question', sprint_id='sprint-a',
                question='BASELINE-QUESTION', answer='BASELINE-ANSWER', asked_by='owner'))
            session.add(SprintHistory(id='export-history', sprint_id='sprint-a', action='updated',
                actor_type='user', actor_id='owner', actor_name='Owner', summary='BASELINE-HISTORY',
                changes=[{'field': 'title', 'old': 'Before', 'new': 'After'}]))
            await session.commit()
            sections = ('base', 'qa', 'evaluations', 'history')
            bundle = await CommunitySqlAlchemyEntityExportReader(session).build_bundle(
                request=EntityExportRequest(board_id='board-a', entity_type=EntityExportType.SPRINT,
                    entity_id='sprint-a', requested_sections=sections),
                disclosure=EntityExportDisclosure(frozenset({'sprint.entity.read', 'sprint.qa.read',
                    'sprint.evaluations.read', 'sprint.history_read'}), sections),
                actor_id='owner', realm_scope=RealmScope.local())
            Path(output).write_text(json.dumps({'provenance': proof, 'bundle': bundle.to_dict()}), encoding='utf-8')
    finally:
        await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
