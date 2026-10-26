"""The performance fixture must retain signed evidence across paired runs."""

from pathlib import Path
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from stress_readiness_fixture import (  # noqa: E402
    _restore_fixture_cache, _save_fixture_cache,
)
from okto_pulse.community.adapters.current_relational_schema import SCHEMA_VERSION  # noqa: E402


def test_fixture_cache_restores_evidence_and_rejects_tampering(tmp_path):
    source = tmp_path / 'source' / 'pulse.db'
    source.parent.mkdir()
    with sqlite3.connect(source) as connection:
        connection.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
        connection.execute('CREATE TABLE fixture_fact (value TEXT NOT NULL)')
        connection.execute("INSERT INTO fixture_fact VALUES ('unchanged')")
    source_evidence = source.parent / 'evidence' / 'receipt' / 'report.json'
    source_evidence.parent.mkdir(parents=True)
    source_evidence.write_text('{"accepted":true}', encoding='utf-8')
    cache = tmp_path / 'cache' / 'pulse.db'
    identity = {'contract': 'test-fixture', 'schema_version': SCHEMA_VERSION}
    digest = _save_fixture_cache(source, cache, identity)
    restored = tmp_path / 'restored' / 'pulse.db'
    restored.parent.mkdir()
    assert _restore_fixture_cache(cache, restored, identity) == digest
    with sqlite3.connect(restored) as connection:
        assert connection.execute('SELECT value FROM fixture_fact').fetchone() == ('unchanged',)
    assert (restored.parent / 'evidence' / 'receipt' / 'report.json').read_bytes() == (
        source_evidence.read_bytes())
    with pytest.raises(ValueError, match='fixture_cache_identity_mismatch'):
        _restore_fixture_cache(cache, tmp_path / 'wrong.db', {'contract': 'wrong'})
    (cache.with_name(cache.name + '.evidence') / 'receipt' / 'report.json').write_text(
        '{"accepted":false}', encoding='utf-8')
    with pytest.raises(ValueError, match='fixture_cache_evidence_digest_mismatch'):
        _restore_fixture_cache(cache, tmp_path / 'tampered.db', identity)
