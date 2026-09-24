"""Historical 0.6.0 remains selectable independently of the current contract."""

from dataclasses import replace

import pytest
from okto_grafx import connect
from okto_pulse.core.kg.logical_transfer import LogicalSchemaError, schema_digest, transfer_logical_graph
from okto_pulse.community.adapters import grafx_recovery_contracts as contracts
from okto_pulse.community.adapters.grafx_schema_v060 import V060_MANIFEST, V060_FINGERPRINT
from logical_transfer_matrix_support import Corpus, MaterializedSource, complete_node


def test_frozen_contracts_are_exact_and_current_v060_is_not_ambiguous(monkeypatch):
    assert V060_MANIFEST.logical_fingerprint == V060_FINGERPRINT
    assert len(contracts.v060_recovery_contract().schema.relation_layouts) == 80
    assert len(contracts.predecessor_recovery_contract().schema.relation_layouts) == 69
    assert len(contracts._contracts('board')) == 3
    frozen = contracts.v060_recovery_contract()
    monkeypatch.setattr(contracts, 'logical_transfer_scope', lambda scope: frozen)
    candidates = contracts._contracts('board')
    assert len(candidates) == 2
    assert len({schema_digest(candidate.schema) for candidate in candidates}) == 2


def test_v060_fingerprint_drift_refuses(monkeypatch):
    monkeypatch.setattr(contracts, 'V060_MANIFEST', replace(V060_MANIFEST, logical_fingerprint='0' * 64))
    with pytest.raises(LogicalSchemaError, match='frozen 0.6.0'):
        contracts.v060_recovery_contract()


def test_v060_real_restore_and_read_remain_exact_when_current_contract_differs(tmp_path, monkeypatch):
    frozen = contracts.v060_recovery_contract()
    # A distinct future catalog is only a fixture; it does not authorize any
    # future production schema or claim an upgrade/migration has happened.
    distinct_schema = replace(frozen.schema, relation_layouts=frozen.schema.relation_layouts[:-1])
    distinct = replace(frozen, schema=distinct_schema, relationship_tables={
        key: value for key, value in frozen.relationship_tables.items()
        if key != frozen.schema.relation_layouts[-1].identity})
    monkeypatch.setattr(contracts, 'logical_transfer_scope', lambda scope: distinct)
    assert len(contracts._contracts('board')) == 3
    node = complete_node(frozen.schema, 'Decision', 'historical-decision', 1)
    corpus = Corpus(frozen.schema, (node,), ())
    target = tmp_path / 'restored-v060'
    sink = contracts.make_grafx_recovery_logical_sink(target, scope='board',
        expected_schema_digest=schema_digest(frozen.schema))
    report = transfer_logical_graph(MaterializedSource(corpus), sink)
    assert report.fingerprint == corpus.fingerprint
    with connect(target, page_size=8192, read_only=True) as database:
        assert contracts.grafx_recovery_contract(database, scope='board') == frozen
        reader = contracts.make_grafx_recovery_logical_source(database, scope='board').open_snapshot()
        try:
            assert reader.schema() == frozen.schema
            actual = tuple(row for batch in reader.iter_nodes(batch_size=10) for row in batch)
            assert actual == (node,)
        finally:
            reader.close()
