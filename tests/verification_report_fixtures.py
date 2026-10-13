"""Authored report data shared by independent current-contract tests."""

from copy import deepcopy

SOURCE = {'reference': 'repo:component', 'revision': 'commit-1', 'sha256': 'a' * 64}

def report(method='inspection'):
    result = {'schema_version': 'verification-report/v1', 'method': method, 'report_id': 'report-1',
        'observed_at': '2026-09-23T13:00:00Z', 'sources': [SOURCE], 'conclusion': 'Public ports only', 'result': 'passed',
        'observations': [{'observation_id': 'o1', 'criterion_id': 'ac-1', 'observation_ref': 'repo:component#imports',
            'expected': 'Public ports', 'observed': 'Imports use ports', 'outcome': 'passed'}]}
    if method == 'inspection':
        result['inspection_procedure'] = SOURCE
    elif method == 'demonstration':
        result.update(procedure=SOURCE, environment=SOURCE)
    else:
        result.update(tool_name='checker', tool_version='1', rules=[SOURCE], configuration=SOURCE,
                      analyzed_scope=['component'], findings=[])
    return deepcopy(result)


async def bind_report_sources(session, value, execution_ids=('execution',)):
    """Positive delivery fixtures name the actual submitted mock source bases."""
    from sqlalchemy import select
    from okto_pulse.community.adapters.sqlalchemy_models import ImplementationTargetExecutionRecordRow

    await session.flush()
    rows = list(await session.scalars(select(ImplementationTargetExecutionRecordRow).where(
        ImplementationTargetExecutionRecordRow.id.in_(execution_ids))))
    assert {row.id for row in rows} == set(execution_ids)
    sources = {(row.source_ref, row.result_declared_revision) for row in rows}
    value['sources'] = [dict(reference=source, revision=revision, sha256='a' * 64)
                        for source, revision in sorted(sources)]
