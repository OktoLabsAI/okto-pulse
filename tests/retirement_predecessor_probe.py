"""Disposable subprocess probe run by the installed predecessor, never a CLI.

It verifies the supplied frozen pair before any runtime initialization. Only the
pytest-created root is used; no server, worker, scheduler or user data is opened.
"""

import asyncio
from dataclasses import replace
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import sysconfig
import zipfile


def provenance():
    site = Path(sysconfig.get_paths()['purelib']).resolve()
    expected = json.loads(Path(os.environ['PULSE_PREDECESSOR_FIXTURE']).read_text())['source_builds']
    results = {}
    for edition, filename in (('core', 'okto_pulse_core'), ('community', 'okto_pulse')):
        repo = Path(os.environ[f'PULSE_PREDECESSOR_{edition.upper()}_REPO']).resolve()
        wheel = Path(os.environ['PULSE_PREDECESSOR_WHEELS']) / f'{filename}-0.3.4-py3-none-any.whl'
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()
        assert head == expected[edition]['commit'] and digest == expected[edition]['wheel_sha256']
        prefix = f'okto_pulse/{edition}/'
        sources = {p.relative_to(repo / 'src').as_posix(): p.read_bytes() for p in (repo / 'src' / prefix).rglob('*.py')}
        installed = {p.relative_to(site).as_posix(): p.read_bytes() for p in (site / prefix).rglob('*.py')}
        assert sources == installed and len(sources) == expected[edition]['python_files']
        with zipfile.ZipFile(wheel) as archive:
            names = [name for name in archive.namelist() if name.startswith(prefix) and not name.endswith('/')]
            for name in names:
                assert archive.read(name) == (repo / 'src' / name).read_bytes() == (site / name).read_bytes()
        assert Path(importlib.util.find_spec(f'okto_pulse.{edition}').origin).resolve().is_relative_to(site)
        results[edition] = {'commit': head, 'wheel_sha256': digest, 'python_files': len(sources), 'payload_files': len(names)}
    return results


async def main():
    proof = provenance()
    mode, root_text, output = sys.argv[1:]
    assert mode in {'seed', 'reopen'}
    root = Path(root_text).resolve(strict=True)
    assert root.name in {'live', 'restored'} and (root / 'source.sqlite3').is_file()
    import okto_pulse.community.app  # noqa: F401 -- Registers the predecessor's ports.
    from okto_pulse.community.adapters import sqlalchemy_database as db
    from okto_pulse.community.adapters.relational_schema_lifecycle import register_community_relational_schema_lifecycle
    from okto_pulse.community.adapters.sqlalchemy_models import Board, Card, Spec, Sprint
    from okto_pulse.community.adapters.graph_backend_binding import CommunityGraphBackendBindingStore
    from okto_pulse.community.adapters.grafx_observations import CommunityGrafxHistory
    from okto_pulse.community.adapters.rebuild_audit_storage import CommunityFileSystemRebuildAuditArtifactStore
    from okto_pulse.core.kg.interfaces.rebuild_audit_storage import RebuildAuditKey
    from okto_pulse.core.services.main import CardService
    from okto_grafx import connect

    runtime = db.configure_community_database(f'sqlite+aiosqlite:///{root / "source.sqlite3"}')
    register_community_relational_schema_lifecycle()
    try:
        await db.init_db()
        async with db.get_session_factory()() as session:
            board = await session.get(Board, 'board-a')
            spec = await session.get(Spec, 'spec-a')
            cards = []
            for suffix in ('a', 'b'):
                card = await session.get(Card, f'card-{suffix}')
                sprint = await session.get(Sprint, card.sprint_id)
                config = CardService(session)._resolve_validation_config(card, spec, sprint, getattr(board, 'settings', None) or {})
                cards.append({'id': card.id, 'title': card.title, 'status': card.status,
                    'sprint_id': card.sprint_id, 'validation': config})
    finally:
        await runtime.close()
    bindings = CommunityGraphBackendBindingStore(root / 'kg')
    graph_path = bindings.board_grafx_path('board-a', 'original')
    key = RebuildAuditKey('run_audit', 'board-a', artifact_id='original-history')
    store = CommunityFileSystemRebuildAuditArtifactStore(root / 'kg')
    if mode == 'seed':
        helpers = runpy.run_path(str(Path(os.environ['PULSE_PREDECESSOR_COMMUNITY_REPO']) / 'tests/logical_transfer_matrix_support.py'))
        corpus = helpers['one_node_corpus']('board', key='original-decision')
        node = corpus.nodes[0]
        corpus = replace(corpus, nodes=(replace(node, properties={**node.properties,
            'source_artifact_ref': 'spec:spec-a', 'created_by_agent': 'system:historical_consolidation'}),))
        graph_path.parent.mkdir(parents=True)
        helpers['seed_generation']('grafx', graph_path, corpus)
        with connect(graph_path, page_size=8192) as graph:
            bindings.initialize_board_binding(board_id='board-a', backend='grafx', generation='original',
                physical_path=graph_path, page_size=8192, database=graph)
            reader = CommunityGrafxHistory(lambda _: graph, lambda *args: None)
            reader.activate('board-a', ('Decision',), (), reason='disposable predecessor rollback proof')
            with graph.begin('write') as writer:
                writer.execute("MATCH (n:Decision {id: 'original-decision'}) SET n.title='First original title'")
            cursor = reader.commits('board-a')['entries'][-1]['commit']
            store.write_json_atomic(key, {'cursor': cursor, 'source': 'original predecessor'})
            with graph.begin('write') as writer:
                writer.execute("MATCH (n:Decision {id: 'original-decision'}) SET n.title='Current original title'")
            graph.checkpoint()  # Seed a cold-readable original before capturing observations.
    binding = bindings.inspect_board_binding('board-a')
    with connect(binding.physical_path, page_size=binding.page_size, read_only=True) as graph:
        reader = CommunityGrafxHistory(lambda _: graph, lambda *args: None)
        audit = store.read_json(key)
        report = {'provenance': proof, 'cards': cards, 'audit': audit, 'commits': reader.commits('board-a'),
            'past': reader.as_of('board-a', audit['cursor'], ('Decision',), ()),
            'current': graph.execute("MATCH (n:Decision {id: 'original-decision'}) RETURN n.title").rows}
    Path(output).write_text(json.dumps(report, sort_keys=True), encoding='utf-8')


if __name__ == '__main__':
    asyncio.run(main())
