"""Real SQLite/WAL + Grafx recovery with interrupted or concurrent capture."""

from contextlib import ExitStack
import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from logical_transfer_matrix_support import (
    export_generation, one_node_corpus, open_generation_database, seed_generation,
)
from okto_pulse.community.adapters import joint_recovery_snapshot as joint
from okto_pulse.community.adapters import recovery_graph_inventory as routing_inventory
from okto_pulse.community.adapters.graph_backend_binding import CommunityGraphBackendBindingStore
from okto_pulse.community.adapters.storage import CommunityFileSystemStorage
from okto_pulse.community.adapters import storage_recovery_snapshot as physical_storage


BUILDS = joint.RecoveryBuildPair("a" * 40, "b" * 40, "c" * 64, "d" * 64)


@pytest.fixture
def sources(tmp_path):
    data = tmp_path / "data"
    recovery = tmp_path / "recovery"
    data.mkdir()
    recovery.mkdir()
    kg = data / "kg"
    kg.mkdir()
    bindings = CommunityGraphBackendBindingStore(kg)
    sql = data / "source.sqlite3"
    with ExitStack() as stack:
        writer = sqlite3.connect(sql)
        stack.callback(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE history(id TEXT PRIMARY KEY, payload BLOB)")
        writer.execute("CREATE TABLE boards(id TEXT PRIMARY KEY)")
        writer.executemany("INSERT INTO boards VALUES (?)", [("board-one",), ("empty-board",)])
        writer.execute("INSERT INTO history VALUES ('sprint-opaque', X'000AFF')")
        writer.commit()
        assert Path(str(sql) + "-wal").stat().st_size > 0
        graphs, corpora = [], []
        for scope in ("board", "global_discovery"):
            corpus = one_node_corpus(scope)
            if scope == "board":
                node = corpus.nodes[0]
                corpus = replace(corpus, nodes=(replace(node, properties={
                    **node.properties, "source_artifact_ref": "sprint:opaque:v1",
                }),))
            path = (bindings.board_grafx_path("board-one", "g1") if scope == "board"
                    else bindings.global_grafx_path("g1"))
            path.parent.mkdir(parents=True, exist_ok=True)
            seed_generation("grafx", path, corpus)
            database = open_generation_database("grafx", path, scope, read_only=False)
            stack.callback(database.close)
            options = dict(backend="grafx", generation="g1", physical_path=path, page_size=8192, database=database)
            if scope == "board":
                bindings.initialize_board_binding(board_id="board-one", **options)
            else:
                bindings.initialize_global_binding(**options)
            graphs.append(joint.RecoveryGraph(database, scope, "board-one" if scope == "board" else None))
            corpora.append(corpus)
        yield sql, tuple(graphs), recovery, data, corpora


def capture(sources, *, snapshot_id="capture", **kwargs):
    sql, graphs, recovery, data, _ = sources
    return joint.create_joint_recovery_snapshot(
        sql, graphs, recovery, snapshot_id=snapshot_id, builds=BUILDS,
        runtime_directories=(data, data / "kg"), max_seconds=120, batch_size=1, **kwargs,
    )


def test_joint_roundtrip_preserves_wal_history_and_both_graph_scopes(sources, tmp_path, monkeypatch):
    sql, graphs, recovery, _, corpora = sources
    real_backup = joint.backup_logical_graph_file
    blocked = []

    def check_sql_reservation(*args, **kwargs):
        with sqlite3.connect(sql, timeout=0.01) as competing:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                competing.execute("INSERT INTO history VALUES ('raced', X'00')")
        blocked.append(True)
        return real_backup(*args, **kwargs)

    monkeypatch.setattr(joint, "backup_logical_graph_file", check_sql_reservation)
    artifact = capture(sources)
    assert blocked == [True, True]
    manifest = joint.verify_joint_recovery_snapshot(artifact)
    assert manifest["builds"] == joint.asdict(BUILDS)
    assert [item["database_uuid"] for item in manifest["graphs"]] == [g.database.identity.database_uuid.hex() for g in graphs]
    assert [item["certificate"]["fingerprint"] for item in manifest["graphs"]] == [c.fingerprint for c in corpora]
    restored = joint.restore_joint_recovery_snapshot(artifact, tmp_path / "restored", builds=BUILDS, max_seconds=120, batch_size=1)
    with sqlite3.connect(restored / "database.sqlite3") as database:
        assert database.execute("SELECT * FROM history").fetchall() == [("sprint-opaque", b"\x00\x0a\xff")]
    for index, corpus in enumerate(corpora):
        observed = export_generation("grafx", restored / f"graph-{index:04d}", scope=corpus.schema.scope)
        assert observed.fingerprint == corpus.fingerprint
        assert observed.counts == corpus.counts
    assert all(not graph.database.closed for graph in graphs)
    assert not list(recovery.glob("*.partial"))
    before = (artifact.directory / "manifest.json").read_bytes()
    with pytest.raises(FileExistsError):
        capture(sources)
    with pytest.raises(FileExistsError):
        joint.restore_joint_recovery_snapshot(artifact, restored, builds=BUILDS)
    assert (artifact.directory / "manifest.json").read_bytes() == before


@pytest.mark.parametrize("when", ["before_first", "after_first", "aba", "external"])
def test_graph_commit_refuses_entire_set_and_releases_sql_reservation(sources, monkeypatch, when):
    sql, graphs, recovery, _, _ = sources
    real_backup = joint.backup_logical_graph_file
    calls = 0
    database = graphs[0 if when == "after_first" else 1].database
    label, field = ("Decision", "title") if when == "after_first" else ("Topic", "name")

    def mutate():
        if when == "external":
            result = subprocess.run([sys.executable, "-c", """
import sys
from okto_grafx import connect
database = connect(sys.argv[1], page_size=8192)
try:
    with database.begin('write') as writer:
        writer.execute("MATCH (n:Topic {id: 'baseline'}) SET n.name = 'external'")
    print('committed')
finally:
    database.close()
""", database.path], capture_output=True, text=True, timeout=30, check=True)
            assert result.stdout.strip() == "committed"
            return
        old = database.execute(f"MATCH (n:{label} {{id: 'baseline'}}) RETURN n.{field}").rows[0][0]
        with database.begin("write") as writer:
            writer.execute(f"MATCH (n:{label} {{id: 'baseline'}}) SET n.{field} = 'concurrent'")
        if when == "aba":
            with database.begin("write") as writer:
                writer.execute("MATCH (n:Topic {id: 'baseline'}) SET n.name = $old", {"old": old})

    def racing_backup(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1 and when == "before_first":
            mutate()
        result = real_backup(*args, **kwargs)
        if calls == 1 and when != "before_first":
            mutate()
        return result

    monkeypatch.setattr(joint, "backup_logical_graph_file", racing_backup)
    with pytest.raises(ValueError, match="graph_changed_during_capture"):
        capture(sources)
    assert not (recovery / "capture").exists()
    assert not list(recovery.glob("*.partial"))
    with sqlite3.connect(sql, timeout=0.01) as writer:
        writer.execute("INSERT INTO history VALUES ('released', X'01')")
    monkeypatch.setattr(joint, "backup_logical_graph_file", real_backup)
    assert capture(sources).directory.exists()


def test_failed_second_graph_does_not_publish_partial_backup(sources, monkeypatch):
    real_backup = joint.backup_logical_graph_file
    calls = 0
    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("graph capture failed")
        return real_backup(*args, **kwargs)
    monkeypatch.setattr(joint, "backup_logical_graph_file", fail_second)
    with pytest.raises(OSError, match="graph capture failed"):
        capture(sources)
    assert not (sources[2] / "capture").exists()
    assert not list(sources[2].glob("*.partial"))


@pytest.mark.parametrize("part", ["manifest", "graph", "sql"])
def test_tamper_refused_before_restore(sources, tmp_path, part):
    artifact = capture(sources)
    filename = {"manifest": "manifest.json", "graph": "graph-0000.jsonl", "sql": "relational/database.sqlite3"}[part]
    path = artifact.directory / filename
    with path.open("ab") as handle:
        handle.write(b"tamper")
    target = tmp_path / "restored"
    with pytest.raises(ValueError, match="hash_mismatch"):
        joint.restore_joint_recovery_snapshot(artifact, target, builds=BUILDS)
    assert not target.exists()


def test_failed_second_restore_removes_unpublished_candidate_set(sources, tmp_path, monkeypatch):
    artifact = capture(sources)
    real_restore = joint.restore_logical_graph_file
    calls = 0
    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("second restore failed")
        return real_restore(*args, **kwargs)
    monkeypatch.setattr(joint, "restore_logical_graph_file", fail_second)
    target = tmp_path / "restored"
    with pytest.raises(OSError, match="second restore failed"):
        joint.restore_joint_recovery_snapshot(artifact, target, builds=BUILDS, max_seconds=120)
    assert not target.exists()
    assert not list(tmp_path.glob("*.restore"))
    joint.verify_joint_recovery_snapshot(artifact)


def test_manifest_path_cannot_redirect_to_another_artifact(sources):
    artifact = capture(sources)
    path = artifact.directory / "manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest["graphs"][0]["file"] = "../foreign.jsonl"
    encoded = json.dumps(manifest).encode()
    path.write_bytes(encoded)
    # Even a structurally malformed artifact with a caller-provided matching
    # digest cannot turn manifest filenames into unrestricted filesystem paths.
    trusted = replace(artifact, manifest_sha256=hashlib.sha256(encoded).hexdigest())
    with pytest.raises(ValueError, match="graph_manifest_invalid"):
        joint.verify_joint_recovery_snapshot(trusted)


def test_invalid_selection_rejected_without_artifact(sources):
    sql, graphs, recovery, data, corpora = sources
    with pytest.raises(ValueError, match="duplicate_graph_scope"):
        capture((sql, (graphs[0], graphs[0]), recovery, data, corpora))
    with pytest.raises(ValueError, match="build_identity_invalid"):
        joint.RecoveryBuildPair("main", "b" * 40, "c" * 64, "d" * 64)
    assert not (recovery / "capture").exists()


def test_restore_requires_the_recorded_build_pair(sources, tmp_path):
    artifact = capture(sources)
    target = tmp_path / "restored"
    with pytest.raises(ValueError, match="build_pair_mismatch"):
        joint.restore_joint_recovery_snapshot(
            artifact, target, builds=replace(BUILDS, core_revision="e" * 40),
        )
    assert not target.exists()
    assert not list(tmp_path.glob("*.restore"))


def test_bound_capture_records_exact_inventory_and_absent_board(sources, tmp_path):
    artifact = capture(sources, kg_base_dir=sources[3] / "kg")
    manifest = joint.verify_joint_recovery_snapshot(artifact)
    assert manifest["format"] == "joint-recovery-snapshot/v2"
    inventory = manifest["routing_inventory"]
    assert inventory["board_ids"] == ["board-one", "empty-board"]
    assert [route["state"] for route in inventory["routes"]] == ["bound", "binding_absent_storage_absent", "bound"]
    assert inventory["issues"] == []
    assert not (sources[3] / "kg" / "boards" / "empty-board").exists()
    restored = joint.restore_joint_recovery_snapshot(artifact, tmp_path / "restored", builds=BUILDS, max_seconds=120)
    assert (restored / "database.sqlite3").exists()


def test_bound_capture_rejects_omitted_active_graph(sources):
    sql, graphs, recovery, data, corpora = sources
    with pytest.raises(ValueError, match="selection_mismatch"):
        capture((sql, (graphs[0],), recovery, data, corpora), kg_base_dir=data / "kg")
    assert not (recovery / "capture").exists()


@pytest.mark.parametrize("change", ["binding", "new_storage", "identity"])
def test_bound_capture_rejects_observed_routing_drift(sources, monkeypatch, change):
    kg = sources[3] / "kg"
    bindings = CommunityGraphBackendBindingStore(kg)
    before = bindings.inspect_board_binding("board-one")
    real_backup = joint.backup_logical_graph_file
    calls = 0
    def change_after_first(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = real_backup(*args, **kwargs)
        if calls == 1:
            if change == "binding":
                # An admitted alternate generation, never selected by this
                # capture, models an independent routed publication.
                candidate = bindings.board_grafx_path("board-one", "g2")
                seed_generation("grafx", candidate, one_node_corpus("board"))
                database = open_generation_database("grafx", candidate, "board", read_only=False)
                try:
                    bindings.compare_and_swap_board_binding(board_id="board-one", expected_binding_sha256=before.binding_sha256,
                        backend="grafx", generation="g2", physical_path=candidate, page_size=8192, database=database)
                finally:
                    database.close()
            elif change == "new_storage":
                (kg / "boards" / "empty-board" / "grafx" / "unbound").mkdir(parents=True)
            else:
                # Mutate only the census observation, not an active engine's
                # identity bytes; corruption of a live DB is not needed here.
                real_digest = routing_inventory._digest
                monkeypatch.setattr(routing_inventory, "_digest",
                    lambda path: "0" * 64 if path == before.physical_path / "grafx.meta" else real_digest(path))
        return result
    monkeypatch.setattr(joint, "backup_logical_graph_file", change_after_first)
    with pytest.raises(ValueError, match="routing_changed_during_capture"):
        capture(sources, kg_base_dir=kg)
    assert not (sources[2] / "capture").exists()


@pytest.fixture
def stored_sources(sources):
    root = sources[3] / "uploads"
    root.mkdir()
    storage = CommunityFileSystemStorage(str(root))
    attachment = Path(asyncio.run(storage.save("board-one", "original.bin", b"\x00attachment\xff")))
    archive = Path(asyncio.run(storage.save("empty-board", "historical-archive.json", b'{ "source": "sprint:opaque" }\r\n')))
    asyncio.run(storage.purge_board("previously-erased"))
    return sources, root, storage, (attachment, archive)


def capture_stored(stored_sources):
    sources, uploads, _, _ = stored_sources
    return capture(sources, kg_base_dir=sources[3] / "kg", storage_root=uploads)


def test_v3_roundtrip_keeps_privacy_fenced_through_final_joint_publish(stored_sources, tmp_path, monkeypatch):
    sources, uploads, _, objects = stored_sources
    real_capture = joint.create_storage_recovery_snapshot
    def capture_under_sql_reservation(*args, **kwargs):
        with sqlite3.connect(sources[0], timeout=0.01) as competing:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                competing.execute("INSERT INTO history VALUES ('raced', X'00')")
        return real_capture(*args, **kwargs)
    monkeypatch.setattr(joint, "create_storage_recovery_snapshot", capture_under_sql_reservation)
    snapshot = capture_stored(stored_sources)
    manifest = joint.verify_joint_recovery_snapshot(snapshot)
    assert manifest["format"] == "joint-recovery-snapshot/v3"
    with pytest.raises(ValueError, match="current_storage_root_required"):
        joint.restore_joint_recovery_snapshot(snapshot, tmp_path / "missing-guard", builds=BUILDS)
    real_publish, checked = joint._publish, []
    def final_publish(stage, target):
        # The callback runs at the final OUTER directory publication, after SQL,
        # cold graph certification and upload copying have already succeeded.
        assert (stage / "database.sqlite3").exists()
        assert (stage / "graph-0000").is_dir()
        assert (stage / "uploads").is_dir()
        script = """
import asyncio, sys
from filelock import FileLock, Timeout
from okto_pulse.community.adapters import storage as module
module.FileLock = lambda path, timeout: FileLock(path, timeout=0.05)
try:
    asyncio.run(module.CommunityFileSystemStorage(sys.argv[1]).purge_board('board-one'))
    print('erased')
except Timeout:
    print('blocked')
"""
        result = subprocess.run([sys.executable, "-c", script, str(uploads)],
            capture_output=True, text=True, timeout=15, check=True)
        assert result.stdout.strip() == "blocked"
        checked.append(True)
        real_publish(stage, target)  # Must work on Windows with SOURCE locks held.
    monkeypatch.setattr(joint, "_publish", final_publish)
    target = joint.restore_joint_recovery_snapshot(snapshot, tmp_path / "restored", builds=BUILDS,
        current_storage_root=uploads, max_seconds=120)
    assert checked == [True]
    with sqlite3.connect(target / "database.sqlite3") as restored:
        assert restored.execute("SELECT * FROM history").fetchall() == [("sprint-opaque", b"\x00\x0a\xff")]
    for index, corpus in enumerate(sources[4]):
        assert export_generation("grafx", target / f"graph-{index:04d}", scope=corpus.schema.scope).fingerprint == corpus.fingerprint
    for original in objects:
        assert (target / "uploads" / original.relative_to(uploads)).read_bytes() == original.read_bytes()
    with pytest.raises(RuntimeError, match="permanently erased"):
        asyncio.run(CommunityFileSystemStorage(str(target / "uploads")).save("previously-erased", "new", b"forbidden"))


def test_v3_later_erasure_refuses_before_any_sql_copy(stored_sources, tmp_path, monkeypatch):
    snapshot = capture_stored(stored_sources)
    asyncio.run(stored_sources[2].purge_board("board-one"))
    monkeypatch.setattr(joint, "restore_sqlite_recovery_snapshot",
        lambda *a, **k: pytest.fail("privacy refusal must precede reconstruction of SQL"))
    with pytest.raises(ValueError, match="newer_erasure_refused"):
        joint.restore_joint_recovery_snapshot(snapshot, tmp_path / "restored", builds=BUILDS,
            current_storage_root=stored_sources[1])
    assert not list(tmp_path.glob("*.restore"))
    assert not (tmp_path / "restored").exists()


def test_v3_erasure_injected_after_payload_copy_refuses_whole_set(stored_sources, tmp_path, monkeypatch):
    snapshot = capture_stored(stored_sources)
    real_copy = physical_storage._StorageRecoveryRestoreGuard.copy_into_new_root
    def changed_after_copy(guard, target):
        real_copy(guard, target)
        digest = hashlib.sha256(b"uncooperative-erasure").hexdigest()
        (stored_sources[1] / ".board_lifecycle" / f"{digest}.erased").write_bytes(b"erased\n")
    monkeypatch.setattr(physical_storage._StorageRecoveryRestoreGuard, "copy_into_new_root", changed_after_copy)
    with pytest.raises(ValueError, match="privacy_state_changed"):
        joint.restore_joint_recovery_snapshot(snapshot, tmp_path / "restored", builds=BUILDS,
            current_storage_root=stored_sources[1], max_seconds=120)
    assert not (tmp_path / "restored").exists()
    assert not list(tmp_path.glob("*.restore"))


def test_v3_graph_commit_during_storage_capture_refuses_whole_backup(stored_sources, monkeypatch):
    real_capture = joint.create_storage_recovery_snapshot
    def changed(*args, **kwargs):
        result = real_capture(*args, **kwargs)
        with stored_sources[0][1][1].database.begin("write") as writer:
            writer.execute("MATCH (n:Topic {id: 'baseline'}) SET n.name = 'during-storage'")
        return result
    monkeypatch.setattr(joint, "create_storage_recovery_snapshot", changed)
    with pytest.raises(ValueError, match="graph_changed_during_capture"):
        capture_stored(stored_sources)
    assert not (stored_sources[0][2] / "capture").exists()


def test_v3_target_overlap_refused_before_touching_upload_namespace(stored_sources):
    snapshot = capture_stored(stored_sources)
    uploads = stored_sources[1]
    before = {str(path.relative_to(uploads)): path.read_bytes() for path in uploads.rglob("*") if path.is_file()}
    with pytest.raises(ValueError, match="restore_root_overlap"):
        joint.restore_joint_recovery_snapshot(snapshot, uploads / "invalid-target", builds=BUILDS,
            current_storage_root=uploads)
    after = {str(path.relative_to(uploads)): path.read_bytes() for path in uploads.rglob("*") if path.is_file()}
    assert after == before
