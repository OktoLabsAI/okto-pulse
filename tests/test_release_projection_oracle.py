"""The installed release oracle must compare every active-set family."""
from dataclasses import replace
from pathlib import Path
import runpy

from okto_pulse.core.application.processors.deterministic_kg import DeterministicWorker


PROBE = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/release_runtime_matrix_probe.py'))


def test_release_projection_oracle_runs_with_current_worker_contract():
    result = PROBE['_kg_projection_parity']()
    assert result['mismatches'] == []
    assert result['artifact_family_count'] == 5
    assert all(family['match'] for family in result['family_projection_hashes'].values())


def test_release_projection_fingerprint_covers_all_active_set_families():
    result = DeterministicWorker().process_spec({
        'id': 'spec', 'board_id': 'board', 'title': 'Spec', 'status': 'done',
        'functional_requirements': [], 'technical_requirements': [],
        'business_rules': [], 'integration_requirements': [],
        'observability_requirements': [], 'api_contracts': [],
        'decisions': [], 'context': '',
        'acceptance_criteria': [{'id': 'ac_one', 'text': 'Expected'}],
        'test_scenarios': [{'id': 'ts_one', 'title': 'Scenario', 'linked_criteria': ['ac_one']}],
    })
    intents = result.relational_projection_active_set_intents
    assert len(intents) > 1
    normalize = PROBE['_normalize_worker_result']
    baseline = normalize(result)
    assert len(baseline['relational_projection_active_sets']) == len(intents)
    assert normalize(replace(result, relational_projection_active_set_intents=tuple(reversed(intents)))) == baseline
    for index in range(len(intents)):
        reduced = replace(result, relational_projection_active_set_intents=intents[:index] + intents[index + 1:])
        assert normalize(reduced) != baseline
