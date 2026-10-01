"""Authored report data shared by independent current-contract tests."""

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
    return result
