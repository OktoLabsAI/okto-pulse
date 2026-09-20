"""A verified recovery set and caller work share one real startup fence."""

import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from okto_pulse.community import serve_lock
from okto_pulse.community.adapters import joint_recovery_snapshot as joint
from test_graph_binding_publication_window import child as publication_attempt
import test_joint_recovery_snapshot as recovery
from logical_transfer_matrix_support import export_generation

sources = recovery.sources
stored_sources = recovery.stored_sources


def _start(root):
    script = """
import sys
from okto_pulse.community import serve_lock
serve_lock._ACQUIRE_MUTEX_TIMEOUT_SECONDS = 0.05
try:
    with serve_lock.ServeInstanceLock(sys.argv[1]).acquire():
        print('started')
except serve_lock.ServeAlreadyRunningError:
    print('blocked')
"""
    result = subprocess.run([sys.executable, "-c", script, str(root)],
        capture_output=True, text=True, timeout=15, check=True)
    return result.stdout.strip()


def _window(sources, **kwargs):
    sql, graphs, directory, data, _ = sources
    return joint.joint_recovery_window(sql, graphs, directory, snapshot_id="capture",
        builds=recovery.BUILDS, runtime_directories=(data, data / "kg"),
        kg_base_dir=data / "kg", max_seconds=120, batch_size=1, **kwargs)


@pytest.mark.parametrize("fail_body", [False, True])
def test_verified_v4_backup_fences_capture_verification_and_body_then_restores_original(
        stored_sources, tmp_path, monkeypatch, fail_body):
    source, uploads, _, objects = stored_sources
    sql, graphs, _, data, corpora = source
    roots = (data, data / "kg")
    original_files = {path.relative_to(uploads): path.read_bytes() for path in objects}
    publish, verify = joint._publish, joint.verify_joint_recovery_snapshot
    observed = []
    body_failure_propagated = False

    def publishing(stage, final):
        assert [_start(root) for root in roots] == ["blocked", "blocked"]
        publish(stage, final)
        observed.append("published")

    def verifying(snapshot, **kwargs):
        assert [_start(root) for root in roots] == ["blocked", "blocked"]
        result = verify(snapshot, **kwargs)
        observed.append("verified")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(joint, "_publish", publishing)
        patch.setattr(joint, "verify_joint_recovery_snapshot", verifying)
        try:
            with _window(source, storage_root=uploads) as artifact:
                assert observed == ["published", "verified"]
                assert json.loads((artifact.directory / "manifest.json").read_bytes())["format"] == "joint-recovery-snapshot/v4"
                assert [_start(root) for root in roots] == ["blocked", "blocked"]
                # Capture locks have ended; the caller owns new SQL/graph work.
                assert publication_attempt(data / "kg") == "entered"
                with sqlite3.connect(sql, timeout=0.05) as connection:
                    connection.execute("UPDATE history SET payload=X'1122'")
                with graphs[0].database.begin("write") as writer:
                    writer.execute("CREATE NODE TABLE MigrationProbe(id STRING, PRIMARY KEY(id))")
                    writer.execute("CREATE (:MigrationProbe {id: 'after-backup'})")
                objects[0].write_bytes(b"later attachment bytes")
                assert [_start(root) for root in roots] == ["blocked", "blocked"]
                if fail_body:
                    raise RuntimeError("interrupted transformation")
        except RuntimeError as failure:
            assert fail_body and str(failure) == "interrupted transformation"
            body_failure_propagated = True

    assert body_failure_propagated is fail_body
    assert [_start(root) for root in roots] == ["started", "started"]
    # Window exit is not automatic rollback or durable cutover admission.
    with sqlite3.connect(sql) as connection:
        assert connection.execute("SELECT payload FROM history").fetchone() == (b"\x11\x22",)
    restored = joint.restore_joint_recovery_snapshot(artifact, tmp_path / "restored",
        builds=recovery.BUILDS, current_storage_root=uploads, max_seconds=120, batch_size=1)
    with sqlite3.connect(restored / "database.sqlite3") as connection:
        assert connection.execute("SELECT * FROM history").fetchall() == [("sprint-opaque", b"\x00\x0a\xff")]
    for index, corpus in enumerate(corpora):
        assert export_generation("grafx", restored / f"graph-{index:04d}", scope=corpus.schema.scope).fingerprint == corpus.fingerprint
    for path, contents in original_files.items():
        assert (restored / "uploads" / path).read_bytes() == contents
    assert all(not graph.database.closed for graph in graphs)


@pytest.mark.parametrize("failure", ["export", "verification"])
def test_failure_before_yield_never_runs_body_and_releases_all_mutexes(sources, monkeypatch, failure):
    if failure == "export":
        def interrupted(*args, **kwargs):
            raise RuntimeError("export interrupted")
        monkeypatch.setattr(joint, "backup_logical_graph_file", interrupted)
    else:
        publish = joint._publish
        def damaged(stage, final):
            publish(stage, final)
            (final / "manifest.json").write_bytes(b"{}")
        monkeypatch.setattr(joint, "_publish", damaged)
    entered = []
    with pytest.raises((RuntimeError, ValueError), match="(export interrupted|manifest_hash_mismatch)"):
        with _window(sources):
            entered.append(True)
    assert entered == []
    assert not list(sources[2].glob("*.partial"))
    assert (sources[2] / "capture").exists() == (failure == "verification")
    assert [_start(root) for root in (sources[3], sources[3] / "kg")] == ["started", "started"]


def test_live_runtime_refuses_before_capture_and_preserves_owner(sources, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("capture must not start while the runtime owns the source")
    monkeypatch.setattr(joint, "_capture_joint_recovery_snapshot", forbidden)
    with serve_lock.ServeInstanceLock(sources[3]).acquire():
        path = Path(sources[3]) / serve_lock.LOCK_FILENAME
        original = path.read_bytes()
        with pytest.raises(serve_lock.ServeAlreadyRunningError):
            with _window(sources):
                pytest.fail("live source admitted")
        assert path.read_bytes() == original
    assert not (sources[2] / "capture").exists()
