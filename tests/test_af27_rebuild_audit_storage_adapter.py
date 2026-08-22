from __future__ import annotations

import json
import multiprocessing
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from okto_pulse.community.adapters.rebuild_audit_storage import (
    CommunityFileSystemRebuildAuditArtifactStore,
    default_community_rebuild_base_dir,
)
from okto_pulse.core.kg.interfaces.rebuild_audit_storage import RebuildAuditKey
from okto_pulse.core.ports.global_discovery_recovery_control import (
    GlobalDiscoveryRecoveryPreparedInputStore,
    GlobalDiscoveryRecoveryWorkerInputStore,
)
from okto_pulse.core.kg.rebuild_generation import (
    RebuildAuditKGGenerationRepository,
)
from repo_layout import resolve_core_repo

ROOT = Path(__file__).resolve().parents[1]


def _configured_checkout_sources() -> tuple[Path, Path]:
    community_src = (ROOT / "src").resolve()
    core_src = (resolve_core_repo(ROOT) / "src").resolve()
    return community_src, core_src


def _is_checkout_source_path(raw: str, selected: frozenset[Path]) -> bool:
    try:
        resolved = Path(raw or os.curdir).resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    if resolved in selected:
        return True
    return resolved.name.casefold() == "src" and any(
        (resolved / "okto_pulse" / edition).is_dir()
        for edition in ("core", "community")
    )


@pytest.fixture
def hermetic_spawn_imports(monkeypatch):
    """Give Windows spawn children one explicit paired-checkout import path."""

    community_src, core_src = _configured_checkout_sources()
    selected = frozenset((community_src, core_src))
    retained = [raw for raw in sys.path if not _is_checkout_source_path(raw, selected)]
    monkeypatch.setattr(
        sys,
        "path",
        [str(community_src), str(core_src), *retained],
    )
    monkeypatch.setenv("OKTO_PULSE_COMMUNITY_REPO", str(ROOT.resolve()))
    monkeypatch.setenv("OKTO_PULSE_CORE_REPO", str(core_src.parent))
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join((str(community_src), str(core_src))),
    )


def _assert_spawned_from_configured_checkouts() -> None:
    community_src, core_src = _configured_checkout_sources()
    modules = (
        (CommunityFileSystemRebuildAuditArtifactStore.__module__, community_src),
        (RebuildAuditKey.__module__, core_src),
    )
    for module_name, expected_src in modules:
        module_file = Path(sys.modules[module_name].__file__).resolve()
        try:
            module_file.relative_to(expected_src)
        except ValueError as exc:
            raise AssertionError(
                f"spawn imported {module_name} from {module_file}, "
                f"expected checkout {expected_src}"
            ) from exc


def _append_from_process(base_dir: str, index: int, start) -> None:
    _assert_spawned_from_configured_checkouts()
    store = CommunityFileSystemRebuildAuditArtifactStore(Path(base_dir))
    key = RebuildAuditKey(
        namespace="global_discovery_recovery",
        board_id="_global",
        artifact_id="multiprocess-cas",
    )
    if not start.wait(timeout=15):
        raise RuntimeError("multiprocess start gate timed out")
    store.replace_json(
        key,
        lambda current: {"items": sorted([*(current or {}).get("items", []), index])},
    )


def _consume_from_process(base_dir: str, start, outcomes) -> None:
    _assert_spawned_from_configured_checkouts()
    store = CommunityFileSystemRebuildAuditArtifactStore(Path(base_dir))
    token_key = RebuildAuditKey(
        namespace="confirmation_token",
        board_id="_global",
        artifact_id="conf-multiprocess",
    )
    receipt_key = RebuildAuditKey(
        namespace="global_discovery_recovery",
        board_id="_global",
        artifact_id="confirmation_receipt_multiprocess",
    )
    if not start.wait(timeout=15):
        raise RuntimeError("multiprocess consume gate timed out")
    outcomes.put(
        store.consume_json_with_receipt(
            source_key=token_key,
            expected_source={"confirmation_id": "conf-multiprocess"},
            receipt_key=receipt_key,
            receipt_payload={"run_id": "multiprocess", "receipt_sha256": "bound"},
        )
    )


def test_af27_community_rebuild_audit_storage_preserves_local_layout(tmp_path):
    store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path)
    board_id = "board-af27"

    event_key = RebuildAuditKey(
        namespace="event_audit",
        board_id=board_id,
        artifact_id="evt_1",
    )
    store.write_json_atomic(event_key, {"event_id": "evt_1"})
    assert (
        tmp_path / "rebuild" / "audit" / "events" / board_id / "evt_1.json"
    ).exists()

    pending_key = RebuildAuditKey(
        namespace="cognitive_pending",
        board_id=board_id,
        kg_generation_id="kg_1",
    )
    store.write_json_atomic(
        pending_key,
        {"board_id": board_id, "kg_generation_id": "kg_1", "items": []},
    )
    replaced = store.replace_json(
        pending_key,
        lambda payload: {**(payload or {}), "pending_count": 2},
    )
    assert replaced["pending_count"] == 2
    assert (
        tmp_path / "rebuild" / "audit" / "cognitive_pending" / board_id / "kg_1.json"
    ).exists()
    assert (
        store.list_json(RebuildAuditKey("cognitive_pending", board_id))[0][
            "kg_generation_id"
        ]
        == "kg_1"
    )

    confirmation_key = RebuildAuditKey(
        namespace="confirmation_audit",
        board_id=board_id,
        artifact_id="audit_1",
    )
    store.write_json_atomic(confirmation_key, {"audit_id": "audit_1"})
    assert (
        tmp_path / "rebuild" / "audit" / "confirmation" / board_id / "audit_1.json"
    ).exists()

    receipt_key = RebuildAuditKey(
        namespace="rebuild_confirmation_receipt",
        board_id=board_id,
        artifact_id="run_1",
    )
    receipt = {
        "schema_version": "kg_rebuild_confirmation_receipt.v1",
        "board_id": board_id,
        "run_id": "run_1",
    }
    store.write_json_atomic(receipt_key, receipt)
    assert store.read_json(receipt_key) == receipt
    assert store.list_json(
        RebuildAuditKey(
            namespace="rebuild_confirmation_receipt",
            board_id=board_id,
        )
    ) == [receipt]
    assert (
        tmp_path
        / "rebuild"
        / "audit"
        / "confirmation_receipts"
        / board_id
        / "run_1.json"
    ).exists()


def test_rebuild_confirmation_receipt_is_atomic_with_token_consumption(tmp_path):
    store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path)
    board_id = "board-receipt"
    token_key = RebuildAuditKey(
        namespace="confirmation_token",
        board_id="_global",
        artifact_id="conf-board-receipt",
    )
    receipt_key = RebuildAuditKey(
        namespace="rebuild_confirmation_receipt",
        board_id=board_id,
        artifact_id="run-board-receipt",
    )
    token = {"confirmation_id": "conf-board-receipt", "expected_board_id": board_id}
    receipt = {
        "schema_version": "kg_rebuild_confirmation_receipt.v1",
        "board_id": board_id,
        "run_id": "run-board-receipt",
    }
    store.write_json_atomic(token_key, token)

    assert (
        store.consume_json_with_receipt(
            source_key=token_key,
            expected_source=token,
            receipt_key=receipt_key,
            receipt_payload=receipt,
        )
        == "consumed"
    )
    assert store.read_json(token_key) is None
    assert store.read_json(receipt_key) == receipt
    assert (
        store.consume_json_with_receipt(
            source_key=token_key,
            expected_source=token,
            receipt_key=receipt_key,
            receipt_payload=receipt,
        )
        == "receipt_exists"
    )


def test_terminal_receipt_rotation_consumes_token_under_one_store_lock(tmp_path):
    store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path)
    token_key = RebuildAuditKey(
        namespace="confirmation_token",
        board_id="_global",
        artifact_id="conf-next",
    )
    active_key = RebuildAuditKey(
        namespace="rebuild_confirmation_receipt",
        board_id="board-rotation",
        artifact_id="active",
    )
    token = {"confirmation_id": "conf-next", "board_id": "board-rotation"}
    terminal = {
        "run_id": "run-old",
        "board_id": "board-rotation",
        "receipt_state": "terminal",
    }
    replacement = {
        "run_id": "run-next",
        "board_id": "board-rotation",
        "receipt_state": "active",
    }
    store.write_json_atomic(token_key, token)
    store.write_json_atomic(active_key, terminal)

    outcome = store.consume_json_replacing_terminal_receipt(
        source_key=token_key,
        expected_source=token,
        receipt_key=active_key,
        expected_terminal_receipt=terminal,
        receipt_payload=replacement,
    )

    assert outcome == "consumed"
    assert store.read_json(token_key) is None
    assert store.read_json(active_key) == replacement


def test_terminal_receipt_rotation_conflict_preserves_marker_and_token(tmp_path):
    store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path)
    token_key = RebuildAuditKey(
        namespace="confirmation_token",
        board_id="_global",
        artifact_id="conf-conflict",
    )
    active_key = RebuildAuditKey(
        namespace="rebuild_confirmation_receipt",
        board_id="board-rotation",
        artifact_id="active",
    )
    token = {"confirmation_id": "conf-conflict"}
    terminal = {"run_id": "run-old", "receipt_state": "terminal"}
    store.write_json_atomic(token_key, token)
    store.write_json_atomic(active_key, terminal)

    outcome = store.consume_json_replacing_terminal_receipt(
        source_key=token_key,
        expected_source=token,
        receipt_key=active_key,
        expected_terminal_receipt={**terminal, "run_id": "forged"},
        receipt_payload={"run_id": "run-next", "receipt_state": "active"},
    )

    assert outcome == "receipt_conflict"
    assert store.read_json(token_key) == token
    assert store.read_json(active_key) == terminal


def test_terminal_receipt_rotation_write_failure_preserves_marker_and_token(
    tmp_path,
    monkeypatch,
):
    store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path)
    token_key = RebuildAuditKey(
        namespace="confirmation_token",
        board_id="_global",
        artifact_id="conf-write-failure",
    )
    active_key = RebuildAuditKey(
        namespace="rebuild_confirmation_receipt",
        board_id="board-rotation",
        artifact_id="active",
    )
    token = {"confirmation_id": "conf-write-failure"}
    terminal = {"run_id": "run-old", "receipt_state": "terminal"}
    store.write_json_atomic(token_key, token)
    store.write_json_atomic(active_key, terminal)

    def fail_receipt_write(_key, _payload):  # noqa: ANN001, ANN202
        raise OSError("injected receipt write failure")

    monkeypatch.setattr(store, "_write_json_atomic_unlocked", fail_receipt_write)

    with pytest.raises(OSError, match="injected receipt write failure"):
        store.consume_json_replacing_terminal_receipt(
            source_key=token_key,
            expected_source=token,
            receipt_key=active_key,
            expected_terminal_receipt=terminal,
            receipt_payload={"run_id": "run-next", "receipt_state": "active"},
        )

    assert store.read_json(token_key) == token
    assert store.read_json(active_key) == terminal


def test_terminal_receipt_rotation_recovers_crash_after_replace_before_unlink(
    tmp_path,
):
    store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path)
    token_key = RebuildAuditKey(
        namespace="confirmation_token",
        board_id="_global",
        artifact_id="conf-crash-cut",
    )
    active_key = RebuildAuditKey(
        namespace="rebuild_confirmation_receipt",
        board_id="board-rotation",
        artifact_id="active",
    )
    token = {"confirmation_id": "conf-crash-cut"}
    terminal = {"run_id": "run-old", "receipt_state": "terminal"}
    replacement = {"run_id": "run-next", "receipt_state": "active"}
    store.write_json_atomic(token_key, token)
    # Exact durable state left by a crash after receipt replacement and before
    # token unlink. The second invocation receives the original CAS witness.
    store.write_json_atomic(active_key, replacement)

    outcome = store.consume_json_replacing_terminal_receipt(
        source_key=token_key,
        expected_source=token,
        receipt_key=active_key,
        expected_terminal_receipt=terminal,
        receipt_payload=replacement,
    )

    assert outcome == "consumed"
    assert store.read_json(token_key) is None
    assert store.read_json(active_key) == replacement


def test_af16_community_generation_storage_preserves_legacy_layout(tmp_path):
    store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path)
    repo = RebuildAuditKGGenerationRepository(artifact_store=store)
    board_id = "board-af16"
    generation_id = "11111111-1111-4111-8111-111111111111"

    result = repo.promote_current(
        board_id=board_id,
        previous_kg_generation_id=None,
        kg_generation_id=generation_id,
        report_ref="rebuild-report:/board-af16/run-1",
        status="completed",
        structural_hash="structural-hash",
        source_hash="source-hash",
        promoted_by="test",
        run_id="run-1",
    )

    assert result.outcome == "promoted"
    assert result.current_kg_generation_id == generation_id
    assert repo.get_current(board_id) == generation_id
    history = repo.load_history(board_id, generation_id)
    assert history is not None
    assert history["kg_generation_id"] == generation_id
    assert (tmp_path / "rebuild" / "generations" / board_id / "current.json").exists()
    assert (
        tmp_path
        / "rebuild"
        / "generations"
        / board_id
        / "history"
        / f"{generation_id}.json"
    ).exists()


def test_af38_community_reindex_and_contingency_layout(tmp_path):
    store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path)
    board_id = "board-af38"
    generation_id = "22222222-2222-4222-8222-222222222222"

    reindex_key = RebuildAuditKey(
        namespace="global_discovery_reindex",
        board_id=board_id,
        kg_generation_id=generation_id,
    )
    store.write_json_atomic(
        reindex_key,
        {
            "board_id": board_id,
            "kg_generation_id": generation_id,
            "status": "reindex_pending",
        },
    )
    assert (
        tmp_path / "rebuild" / "discovery_reindex" / board_id / f"{generation_id}.json"
    ).exists()
    assert store.read_json(reindex_key)["status"] == "reindex_pending"

    contingency_key = RebuildAuditKey(
        namespace="contingency",
        board_id=board_id,
        artifact_id="contingency_af38",
    )
    store.write_json_atomic(
        contingency_key,
        {
            "board_id": board_id,
            "contingency_id": "contingency_af38",
            "quarantine_ids": ["q_af38"],
        },
    )
    assert (tmp_path / "contingency" / "contingency_af38" / "contingency.json").exists()
    rows = store.list_json(RebuildAuditKey(namespace="contingency", board_id=board_id))
    assert [row["contingency_id"] for row in rows] == ["contingency_af38"]
    assert not list(tmp_path.rglob("*.tmp"))


def test_af38_community_reads_existing_reindex_and_contingency_artifacts(tmp_path):
    store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path)
    board_id = "board-af38-history"
    generation_id = "33333333-3333-4333-8333-333333333333"

    historical_reindex = (
        tmp_path / "rebuild" / "discovery_reindex" / board_id / f"{generation_id}.json"
    )
    historical_reindex.parent.mkdir(parents=True)
    historical_reindex.write_text(
        json.dumps(
            {
                "board_id": board_id,
                "kg_generation_id": generation_id,
                "status": "reindexed",
                "recorded_at": "2026-07-08T09:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    reindex_key = RebuildAuditKey(
        namespace="global_discovery_reindex",
        board_id=board_id,
        kg_generation_id=generation_id,
    )
    assert store.read_json(reindex_key)["status"] == "reindexed"
    assert (
        store.list_json(
            RebuildAuditKey(namespace="global_discovery_reindex", board_id=board_id)
        )[0]["kg_generation_id"]
        == generation_id
    )

    historical_contingency = (
        tmp_path / "contingency" / "contingency_history" / "contingency.json"
    )
    historical_contingency.parent.mkdir(parents=True)
    historical_contingency.write_text(
        json.dumps(
            {
                "board_id": board_id,
                "contingency_id": "contingency_history",
                "quarantine_ids": ["q_history"],
            }
        ),
        encoding="utf-8",
    )

    contingency_key = RebuildAuditKey(
        namespace="contingency",
        board_id=board_id,
        artifact_id="contingency_history",
    )
    assert store.read_json(contingency_key)["quarantine_ids"] == ["q_history"]
    assert store.delete_json(contingency_key) is True
    assert store.delete_json(contingency_key) is False


def test_af16_community_rebuild_base_dir_uses_typed_kg_root(tmp_path):
    configured = tmp_path / "configured-kg-root"

    assert default_community_rebuild_base_dir(configured) == configured
    assert configured.exists()


def test_rebuild_store_survives_restart_at_same_typed_kg_root(tmp_path):
    root = default_community_rebuild_base_dir(tmp_path / "durable-kg")
    key = RebuildAuditKey(
        namespace="global_discovery_recovery",
        board_id="_global",
        artifact_id="restart-proof",
    )
    CommunityFileSystemRebuildAuditArtifactStore(root).write_json_atomic(
        key, {"state": "running"}
    )

    restarted = CommunityFileSystemRebuildAuditArtifactStore(root)
    assert restarted.read_json(key) == {"state": "running"}
    assert str(restarted._base_dir).startswith(str(root))  # noqa: SLF001


def test_replace_json_is_serialized_across_spawned_processes(
    tmp_path,
    hermetic_spawn_imports,
):
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    processes = [
        context.Process(
            target=_append_from_process,
            args=(str(tmp_path), index, start),
        )
        for index in range(6)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0

    store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path)
    key = RebuildAuditKey(
        namespace="global_discovery_recovery",
        board_id="_global",
        artifact_id="multiprocess-cas",
    )
    assert store.read_json(key) == {"items": list(range(6))}
    assert not list(tmp_path.rglob(".*.tmp"))


def test_confirmation_receipt_consume_is_cross_instance_exactly_once(tmp_path):
    first = CommunityFileSystemRebuildAuditArtifactStore(tmp_path)
    second = CommunityFileSystemRebuildAuditArtifactStore(tmp_path)
    token_key = RebuildAuditKey(
        namespace="confirmation_token",
        board_id="_global",
        artifact_id="conf-cross-instance",
    )
    receipt_key = RebuildAuditKey(
        namespace="global_discovery_recovery",
        board_id="_global",
        artifact_id="confirmation_receipt_run-1",
    )
    token = {"confirmation_id": "conf-cross-instance", "scope": "_global"}
    receipt = {"run_id": "run-1", "receipt_sha256": "bound"}
    first.write_json_atomic(token_key, token)
    gate = threading.Barrier(2)

    def consume(store):
        gate.wait(timeout=5)
        return store.consume_json_with_receipt(
            source_key=token_key,
            expected_source=token,
            receipt_key=receipt_key,
            receipt_payload=receipt,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(consume, (first, second)))

    assert sorted(outcomes) == ["consumed", "receipt_exists"]
    assert first.read_json(token_key) is None
    assert second.read_json(receipt_key) == receipt


def test_confirmation_receipt_consume_is_exactly_once_across_processes(
    tmp_path,
    hermetic_spawn_imports,
):
    store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path)
    token_key = RebuildAuditKey(
        namespace="confirmation_token",
        board_id="_global",
        artifact_id="conf-multiprocess",
    )
    store.write_json_atomic(token_key, {"confirmation_id": "conf-multiprocess"})
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    outcomes = context.Queue()
    processes = [
        context.Process(
            target=_consume_from_process,
            args=(str(tmp_path), start, outcomes),
        )
        for _index in range(4)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0

    observed = sorted(outcomes.get(timeout=5) for _process in processes)
    assert observed == ["consumed", *("receipt_exists" for _index in range(3))]


def test_atomic_write_orders_file_fsync_replace_and_directory_fsync(
    tmp_path, monkeypatch
):
    from okto_pulse.community.adapters import rebuild_audit_storage as module

    events: list[str] = []
    real_fsync = module.os.fsync
    real_replace = module._replace_write_through
    real_dir_fsync = module._fsync_directory

    def fsync(descriptor):
        events.append("file_fsync")
        return real_fsync(descriptor)

    def replace(source, destination):
        events.append("replace")
        return real_replace(source, destination)

    def fsync_dir(path):
        events.append("directory_fsync")
        return real_dir_fsync(path)

    monkeypatch.setattr(module.os, "fsync", fsync)
    monkeypatch.setattr(module, "_replace_write_through", replace)
    monkeypatch.setattr(module, "_fsync_directory", fsync_dir)
    store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path)
    key = RebuildAuditKey(
        namespace="global_discovery_recovery",
        board_id="_global",
        artifact_id="durability-order",
    )
    store.write_json_atomic(key, {"complete": True})

    assert events.index("file_fsync") < events.index("replace")
    assert events.index("replace") < events.index("directory_fsync")


def _synthetic_windows_error(code: int) -> OSError:
    exc = OSError(f"synthetic Windows error {code}")
    exc.winerror = code
    return exc


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing semantics")
def test_atomic_write_retries_transient_windows_sharing_denial(tmp_path, monkeypatch):
    from okto_pulse.community.adapters import rebuild_audit_storage as module

    store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path)
    key = RebuildAuditKey(
        namespace="global_discovery_recovery",
        board_id="_global",
        artifact_id="windows-sharing-retry",
    )
    store.write_json_atomic(key, {"version": "old"})
    path = Path(store.reference(key))
    attempts = 0
    sleeps: list[float] = []

    def replace(source, destination):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise _synthetic_windows_error(5)
        module.os.replace(source, destination)

    monkeypatch.setattr(module, "_windows_replace_write_through_once", replace)
    monkeypatch.setattr(module.time, "sleep", sleeps.append)

    store.write_json_atomic(key, {"version": "new"})

    assert attempts == 2
    assert sleeps == [0.05]
    assert store.read_json(key) == {"version": "new"}
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing semantics")
def test_atomic_write_exhausts_windows_sharing_retry_without_replacing(
    tmp_path, monkeypatch
):
    from okto_pulse.community.adapters import rebuild_audit_storage as module

    store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path)
    key = RebuildAuditKey(
        namespace="global_discovery_recovery",
        board_id="_global",
        artifact_id="windows-sharing-exhausted",
    )
    store.write_json_atomic(key, {"version": "committed"})
    path = Path(store.reference(key))
    before = path.read_bytes()
    attempts = 0
    sleeps: list[float] = []

    def replace(_source, _destination):
        nonlocal attempts
        attempts += 1
        raise _synthetic_windows_error(32)

    monkeypatch.setattr(module, "_windows_replace_write_through_once", replace)
    monkeypatch.setattr(module.time, "sleep", sleeps.append)

    with pytest.raises(OSError) as raised:
        store.write_json_atomic(key, {"version": "uncommitted"})

    assert raised.value.winerror == 32
    assert attempts == 3
    assert sleeps == [0.05, 0.10]
    assert path.read_bytes() == before
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing semantics")
def test_atomic_write_does_not_retry_other_windows_errors(tmp_path, monkeypatch):
    from okto_pulse.community.adapters import rebuild_audit_storage as module

    store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path)
    key = RebuildAuditKey(
        namespace="global_discovery_recovery",
        board_id="_global",
        artifact_id="windows-non-sharing-error",
    )
    attempts = 0
    sleeps: list[float] = []

    def replace(_source, _destination):
        nonlocal attempts
        attempts += 1
        raise _synthetic_windows_error(87)

    monkeypatch.setattr(module, "_windows_replace_write_through_once", replace)
    monkeypatch.setattr(module.time, "sleep", sleeps.append)

    with pytest.raises(OSError) as raised:
        store.write_json_atomic(key, {"version": "never-committed"})

    assert raised.value.winerror == 87
    assert attempts == 1
    assert sleeps == []


def test_next_mutation_removes_orphan_unique_temp_without_promoting_it(tmp_path):
    store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path)
    key = RebuildAuditKey(
        namespace="global_discovery_recovery",
        board_id="_global",
        artifact_id="orphan-recovery",
    )
    store.write_json_atomic(key, {"version": "old"})
    path = Path(store.reference(key))
    orphan = path.with_name(f".{path.name}.interrupted.tmp")
    orphan.write_text('{"version":"uncommitted"}', encoding="utf-8")

    store.write_json_atomic(key, {"version": "new"})

    assert store.read_json(key) == {"version": "new"}
    assert not orphan.exists()


def test_replace_callback_failure_leaves_previous_complete_document(tmp_path):
    store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path)
    key = RebuildAuditKey(
        namespace="global_discovery_recovery",
        board_id="_global",
        artifact_id="callback-failure",
    )
    store.write_json_atomic(key, {"version": "committed"})

    def fail(_current):
        raise RuntimeError("synthetic transform failure")

    try:
        store.replace_json(key, fail)
    except RuntimeError as exc:
        assert str(exc) == "synthetic transform failure"
    else:
        raise AssertionError("replace_json must propagate callback failures")

    assert store.read_json(key) == {"version": "committed"}


def test_bounded_list_refuses_truncation_and_oversized_documents(tmp_path):
    store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path)
    board_id = "board-bounded-overlay"
    for index in range(3):
        store.write_json_atomic(
            RebuildAuditKey(
                namespace="cognitive_pending",
                board_id=board_id,
                kg_generation_id=f"generation-{index}",
            ),
            {"kg_generation_id": f"generation-{index}", "items": []},
        )

    with pytest.raises(RuntimeError, match="result_limit_exceeded"):
        store.list_json_bounded(
            RebuildAuditKey(namespace="cognitive_pending", board_id=board_id),
            max_results=2,
            max_document_bytes=1024,
        )

    exact = RebuildAuditKey(
        namespace="cognitive_pending",
        board_id=board_id,
        kg_generation_id="generation-0",
    )
    with pytest.raises(RuntimeError, match="document_limit_exceeded"):
        store.list_json_bounded(
            exact,
            max_results=1,
            max_document_bytes=8,
        )


def test_revisioned_replace_persists_pending_target_committed_order(
    tmp_path,
    monkeypatch,
):
    store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path)
    target_key = RebuildAuditKey(
        namespace="cognitive_pending",
        board_id="board-revision-order",
        kg_generation_id="generation-1",
    )
    revision_key = RebuildAuditKey(
        namespace="global_discovery_recovery",
        board_id="_global",
        artifact_id="cognitive_pending_overlay_revision",
    )
    writes: list[tuple[RebuildAuditKey, str]] = []
    original = store._write_json_atomic_unlocked  # noqa: SLF001

    def recording_write(key, payload):  # noqa: ANN001
        writes.append((key, str(payload.get("state") or "target")))
        original(key, payload)

    monkeypatch.setattr(store, "_write_json_atomic_unlocked", recording_write)
    target, revision = store.replace_json_with_revision(
        key=target_key,
        transform=lambda _current: {"items": []},
        revision_key=revision_key,
        revision_transition=lambda _current: (
            {"version": 1, "state": "mutating", "revision": 1, "nonce": "n" * 24},
            {"version": 1, "state": "stable", "revision": 1, "nonce": "n" * 24},
        ),
    )

    assert target == {"items": []}
    assert revision["state"] == "stable"
    assert [(key, state) for key, state in writes] == [
        (revision_key, "mutating"),
        (target_key, "target"),
        (revision_key, "stable"),
    ]


def test_af16_composition_uses_community_rebuild_base_dir_resolver():
    composition_source = ROOT / "src/okto_pulse/community/adapters/composition.py"
    source = composition_source.read_text(encoding="utf-8")
    forbidden_import = (
        "from okto_pulse.core.kg."
        + "rebuild_audit import "
        + "default_"
        + "rebuild_base_dir"
    )

    assert "default_community_rebuild_base_dir" in source
    assert forbidden_import not in source


def test_recovery_run_artifact_paths_never_embed_untrusted_run_id(tmp_path):
    store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path)
    malicious = "x/../../../../escaped"
    keys = (
        GlobalDiscoveryRecoveryPreparedInputStore._key(malicious, 1),  # noqa: SLF001
        GlobalDiscoveryRecoveryWorkerInputStore._key(malicious, 1),  # noqa: SLF001
    )

    for key in keys:
        resolved = Path(store.reference(key)).resolve(strict=False)
        resolved.relative_to(tmp_path.resolve(strict=False))
        assert malicious not in str(resolved)

    with pytest.raises(ValueError, match="safe logical identifier"):
        RebuildAuditKey(
            namespace="global_discovery_recovery",
            board_id="_global",
            artifact_id=malicious,
        )

    with pytest.raises(ValueError):
        store._contained_path(tmp_path.parent / "escaped.json")  # noqa: SLF001
