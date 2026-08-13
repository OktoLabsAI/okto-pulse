from __future__ import annotations

import json
import multiprocessing
import os
import time
from pathlib import Path

import pytest

from okto_pulse.community.adapters.coordination import CommunityLocalWriteLockPort
from okto_pulse.community.adapters.rebuild_audit_storage import (
    CommunityFileSystemRebuildAuditArtifactStore,
)
from okto_pulse.community.adapters.local_storage_ref import local_storage_ref
from okto_pulse.core.kg.quarantine import (
    KGQuarantineService,
    QuarantineError,
    QuarantineErrorCode,
)
from okto_pulse.core.kg.single_writer_lock import (
    KGSingleWriterLock,
    LOCK_FILENAME,
    RECOVERY_LOCK_FILENAME,
    SingleWriterLockError,
)


def test_af29_s2_community_single_writer_preserves_local_semantics(tmp_path: Path):
    port = CommunityLocalWriteLockPort()
    lock = KGSingleWriterLock(
        base_dir=tmp_path / "locks",
        write_lock_port=port,
    )

    first = lock.acquire(
        board_id="board-a",
        operation="rebuild",
        owner_id="worker-a",
        ttl_seconds=30,
        admin_lane=True,
    )
    assert first.acquired is True
    assert first.owner_token
    assert first.admin_lane is True

    contender = lock.acquire(
        board_id="board-a",
        operation="consolidate",
        owner_id="worker-b",
        ttl_seconds=30,
    )
    assert contender.acquired is False
    assert contender.current_owner == "worker-a"
    assert contender.admin_lane is True

    assert lock.release(board_id="board-a", owner_token="wrong-token") is False
    assert lock.is_owner("board-a", first.owner_token)
    assert lock.release(board_id="board-a", owner_token=first.owner_token) is True

    dead = lock.acquire(
        board_id="board-stale",
        operation="consolidate",
        owner_id="dead-worker",
        ttl_seconds=1,
    )
    assert dead.acquired is True
    time.sleep(1.05)
    recovered = lock.acquire(
        board_id="board-stale",
        operation="consolidate",
        owner_id="rescuer",
        ttl_seconds=30,
    )
    assert recovered.acquired is True
    assert recovered.stale_recovered is True
    assert lock.release(
        board_id="board-stale",
        owner_token=recovered.owner_token or "",
    )

    blocked = lock.acquire(
        board_id="board-recovery-marker",
        operation="consolidate",
        owner_id="dead-worker",
        ttl_seconds=1,
    )
    assert blocked.acquired is True
    time.sleep(1.05)
    marker = tmp_path / "locks" / "board-recovery-marker" / RECOVERY_LOCK_FILENAME
    marker.write_text(
        json.dumps(
            {
                "owner_id": "crashed-recoverer",
                "operation": "recover",
                "board_id": "board-recovery-marker",
                "acquired_at_epoch": time.time() - 60,
                "expires_at_epoch": time.time() - 30,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(SingleWriterLockError) as exc_info:
        lock.acquire(
            board_id="board-recovery-marker",
            operation="consolidate",
            owner_id="rescuer",
            ttl_seconds=30,
        )
    assert exc_info.value.retryable is False


def _community_lock(tmp_path: Path) -> KGSingleWriterLock:
    return KGSingleWriterLock(
        base_dir=tmp_path / "locks",
        write_lock_port=CommunityLocalWriteLockPort(),
    )


def _write_protocol_recovery_marker(
    path: Path,
    *,
    board_id: str,
    owner_id: str = "crashed-recoverer",
) -> str:
    from okto_pulse.community.adapters import coordination

    token = "recovery-token-from-crashed-process"
    path.write_text(
        json.dumps(
            {
                "schema_version": coordination._SINGLE_WRITER_RECOVERY_SCHEMA_VERSION,
                "protocol": coordination._SINGLE_WRITER_RECOVERY_PROTOCOL,
                "recovery_token": token,
                "owner_id": owner_id,
                "operation": "recover",
                "board_id": board_id,
                "acquired_at_epoch": time.time() - 60,
                "expires_at_epoch": time.time() + 30,
            }
        ),
        encoding="utf-8",
    )
    return token


def _multiprocess_recoverer(
    base_dir: str,
    owner_id: str,
    start_event,
    results,
) -> None:
    lock = KGSingleWriterLock(
        base_dir=base_dir,
        write_lock_port=CommunityLocalWriteLockPort(),
    )
    start_event.wait(10)
    try:
        acquisition = lock.acquire(
            board_id="board-process-race",
            operation="rebuild",
            owner_id=owner_id,
            ttl_seconds=60,
        )
        results.put(
            {
                "owner_id": owner_id,
                "acquired": acquisition.acquired,
                "current_owner": acquisition.current_owner,
            }
        )
    except BaseException as exc:  # pragma: no cover - child-process evidence
        results.put({"owner_id": owner_id, "error": repr(exc)})


def _multiprocess_crash_inside_recovery(base_dir: str, ready_event) -> None:
    port = CommunityLocalWriteLockPort()
    board_dir = Path(base_dir) / "board-protocol-crash"
    board_dir.mkdir(parents=True, exist_ok=True)
    recovery_path = board_dir / RECOVERY_LOCK_FILENAME
    with port._single_writer_recovery_mutex(board_dir):  # noqa: SLF001
        token, _ = port._claim_single_writer_recovery_marker(  # noqa: SLF001
            recovery_path=recovery_path,
            board_id="board-protocol-crash",
            operation="recover",
            owner_id="crashed-recoverer",
        )
        assert token
        ready_event.set()
        os._exit(0)


def test_af29_s2_community_write_lock_manifest_is_human_readable_json(
    tmp_path: Path,
):
    lock = _community_lock(tmp_path)
    acquired = lock.acquire(
        board_id="board-json",
        operation="rebuild",
        owner_id="worker-json",
        ttl_seconds=60,
        admin_lane=True,
    )
    assert acquired.acquired is True

    manifest_path = tmp_path / "locks" / "board-json" / LOCK_FILENAME
    decoded = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert decoded["owner_id"] == "worker-json"
    assert decoded["operation"] == "rebuild"
    assert decoded["admin_lane"] is True
    for key in (
        "owner_token",
        "acquired_at_epoch",
        "expires_at_epoch",
        "acquired_at",
        "expires_at",
    ):
        assert key in decoded
    assert lock.release(
        board_id="board-json",
        owner_token=str(acquired.owner_token),
    )
    assert not manifest_path.exists()
    # The kernel-mutex rendezvous pathname is intentionally persistent. Removing
    # it could create a second inode while another process waits on the first.
    assert manifest_path.parent.exists()


def test_af29_s2_community_stale_recovery_cas_preserves_fresh_primary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    port = CommunityLocalWriteLockPort()
    lock = KGSingleWriterLock(base_dir=tmp_path / "locks", write_lock_port=port)
    stale = lock.acquire(
        board_id="board-cas",
        operation="rebuild",
        owner_id="dead-worker",
        ttl_seconds=1,
    )
    assert stale.acquired is True
    time.sleep(1.05)

    primary_path = tmp_path / "locks" / "board-cas" / LOCK_FILENAME
    fresh_manifest = type(lock.inspect(board_id="board-cas"))(
        owner_token="fresh-token",
        owner_id="fresh-recoverer",
        operation="rebuild",
        acquired_at_epoch=time.time(),
        expires_at_epoch=time.time() + 60,
        admin_lane=False,
    )
    real_read = port._read_single_writer_manifest
    call_count = {"n": 0}

    def patched_read(path: Path):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return real_read(path)
        if call_count["n"] == 2:
            return fresh_manifest
        return real_read(path)

    real_unlink = Path.unlink
    primary_unlinks = {"n": 0}

    def spy_unlink(path: Path, *args, **kwargs):
        if path == primary_path:
            primary_unlinks["n"] += 1
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(port, "_read_single_writer_manifest", patched_read)
    monkeypatch.setattr(Path, "unlink", spy_unlink)

    result = lock.acquire(
        board_id="board-cas",
        operation="rebuild",
        owner_id="rescuer",
        ttl_seconds=60,
    )
    assert result.acquired is False
    assert result.current_owner == "fresh-recoverer"
    assert primary_unlinks["n"] == 0
    assert primary_path.exists()


def test_af29_s2_community_recovery_lock_serializes_fresh_holder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    lock = _community_lock(tmp_path)
    assert lock.acquire(
        board_id="board-serial",
        operation="rebuild",
        owner_id="dead-worker",
        ttl_seconds=1,
    ).acquired
    time.sleep(1.05)

    board_dir = tmp_path / "locks" / "board-serial"
    primary_path = board_dir / LOCK_FILENAME
    recovery_path = board_dir / RECOVERY_LOCK_FILENAME
    recovery_path.write_text(
        json.dumps(
            {
                "owner_id": "recoverer-a",
                "operation": "rebuild",
                "board_id": "board-serial",
                "acquired_at_epoch": time.time(),
                "expires_at_epoch": time.time() + 30,
            }
        ),
        encoding="utf-8",
    )

    real_unlink = Path.unlink
    primary_unlinks = {"n": 0}

    def spy_unlink(path: Path, *args, **kwargs):
        if path == primary_path:
            primary_unlinks["n"] += 1
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", spy_unlink)
    with pytest.raises(SingleWriterLockError) as exc_info:
        lock.acquire(
            board_id="board-serial",
            operation="rebuild",
            owner_id="recoverer-b",
            ttl_seconds=60,
        )

    assert "recovery_lock_stale_manual_intervention_required" in (
        exc_info.value.reason
    )
    assert primary_unlinks["n"] == 0
    assert primary_path.exists()
    assert recovery_path.exists()


def test_af29_s2_community_stale_recovery_lock_requires_manual_intervention(
    tmp_path: Path,
):
    lock = _community_lock(tmp_path)
    assert lock.acquire(
        board_id="board-manual",
        operation="rebuild",
        owner_id="dead-primary",
        ttl_seconds=1,
    ).acquired
    time.sleep(1.05)

    board_dir = tmp_path / "locks" / "board-manual"
    primary_path = board_dir / LOCK_FILENAME
    recovery_path = board_dir / RECOVERY_LOCK_FILENAME
    primary_before = primary_path.read_bytes()
    recovery_payload = {
        "owner_id": "dead-recoverer",
        "operation": "rebuild",
        "board_id": "board-manual",
        "acquired_at_epoch": time.time() - 60,
        "expires_at_epoch": time.time() - 30,
    }
    recovery_path.write_text(json.dumps(recovery_payload), encoding="utf-8")
    recovery_before = recovery_path.read_bytes()

    with pytest.raises(SingleWriterLockError) as exc_info:
        lock.acquire(
            board_id="board-manual",
            operation="rebuild",
            owner_id="rescuer",
            ttl_seconds=60,
        )
    assert exc_info.value.retryable is False
    assert "recovery_lock_stale_manual_intervention_required" in exc_info.value.reason
    assert "dead-recoverer" in exc_info.value.reason
    assert primary_path.read_bytes() == primary_before
    assert recovery_path.read_bytes() == recovery_before


def test_af29_s2_community_concurrent_stale_recoverers_do_not_corrupt_primary(
    tmp_path: Path,
):
    lock_a = _community_lock(tmp_path)
    assert lock_a.acquire(
        board_id="board-concurrent",
        operation="rebuild",
        owner_id="dead-primary",
        ttl_seconds=1,
    ).acquired
    time.sleep(1.05)

    board_dir = tmp_path / "locks" / "board-concurrent"
    primary_path = board_dir / LOCK_FILENAME
    recovery_path = board_dir / RECOVERY_LOCK_FILENAME
    recovery_path.write_text(
        json.dumps(
            {
                "owner_id": "dead-recoverer",
                "operation": "rebuild",
                "board_id": "board-concurrent",
                "acquired_at_epoch": time.time() - 60,
                "expires_at_epoch": time.time() - 30,
            }
        ),
        encoding="utf-8",
    )
    primary_before = primary_path.read_bytes()
    recovery_before = recovery_path.read_bytes()

    lock_b = _community_lock(tmp_path)
    with pytest.raises(SingleWriterLockError) as exc_a:
        lock_a.acquire(
            board_id="board-concurrent",
            operation="rebuild",
            owner_id="recoverer-a",
            ttl_seconds=60,
        )
    with pytest.raises(SingleWriterLockError) as exc_b:
        lock_b.acquire(
            board_id="board-concurrent",
            operation="rebuild",
            owner_id="recoverer-b",
            ttl_seconds=60,
        )
    assert "recovery_lock_stale_manual_intervention_required" in exc_a.value.reason
    assert "recovery_lock_stale_manual_intervention_required" in exc_b.value.reason
    assert primary_path.read_bytes() == primary_before
    assert recovery_path.read_bytes() == recovery_before


def test_protocol_marker_from_crashed_recoverer_is_reclaimed_under_kernel_mutex(
    tmp_path: Path,
) -> None:
    lock = _community_lock(tmp_path)
    assert lock.acquire(
        board_id="board-protocol-crash",
        operation="rebuild",
        owner_id="dead-primary",
        ttl_seconds=1,
    ).acquired
    time.sleep(1.05)

    board_dir = tmp_path / "locks" / "board-protocol-crash"
    recovery_path = board_dir / RECOVERY_LOCK_FILENAME
    context = multiprocessing.get_context("spawn")
    ready_event = context.Event()
    crashed = context.Process(
        target=_multiprocess_crash_inside_recovery,
        args=(str(tmp_path / "locks"), ready_event),
    )
    crashed.start()
    assert ready_event.wait(20)
    crashed.join(timeout=20)
    assert crashed.exitcode == 0
    assert recovery_path.exists()

    recovered = lock.acquire(
        board_id="board-protocol-crash",
        operation="rebuild",
        owner_id="rescuer",
        ttl_seconds=60,
    )

    assert recovered.acquired is True
    assert recovered.stale_recovered is True
    assert not recovery_path.exists()


def test_kernel_mutex_allows_only_one_multiprocess_stale_recoverer(
    tmp_path: Path,
) -> None:
    base_dir = tmp_path / "locks"
    lock = KGSingleWriterLock(
        base_dir=base_dir,
        write_lock_port=CommunityLocalWriteLockPort(),
    )
    assert lock.acquire(
        board_id="board-process-race",
        operation="rebuild",
        owner_id="dead-primary",
        ttl_seconds=1,
    ).acquired
    time.sleep(1.05)

    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_multiprocess_recoverer,
            args=(str(base_dir), owner_id, start_event, results),
        )
        for owner_id in ("recoverer-a", "recoverer-b")
    ]
    for process in processes:
        process.start()
    start_event.set()
    observed = [results.get(timeout=20) for _ in processes]
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    assert not [item for item in observed if "error" in item]
    assert sum(bool(item["acquired"]) for item in observed) == 1
    manifest = lock.inspect(board_id="board-process-race")
    assert manifest is not None
    assert manifest.owner_id in {"recoverer-a", "recoverer-b"}


def test_live_kernel_mutex_blocks_acquire_renew_and_release_without_mutation(
    tmp_path: Path,
) -> None:
    port = CommunityLocalWriteLockPort()
    base_dir = tmp_path / "locks"
    lock = KGSingleWriterLock(base_dir=base_dir, write_lock_port=port)
    acquired = lock.acquire(
        board_id="board-live-mutex",
        operation="rebuild",
        owner_id="owner-a",
        ttl_seconds=60,
    )
    assert acquired.acquired and acquired.owner_token
    board_dir = base_dir / "board-live-mutex"
    primary_path = board_dir / LOCK_FILENAME
    primary_before = primary_path.read_bytes()

    with port._single_writer_recovery_mutex(board_dir):  # noqa: SLF001
        contender = lock.acquire(
            board_id="board-live-mutex",
            operation="rebuild",
            owner_id="owner-b",
            ttl_seconds=60,
        )
        renewed = lock.renew(
            board_id="board-live-mutex",
            owner_token=str(acquired.owner_token),
            ttl_seconds=60,
        )
        released = lock.release(
            board_id="board-live-mutex",
            owner_token=str(acquired.owner_token),
        )

    assert contender.acquired is False
    assert renewed is False
    assert released is False
    assert primary_path.read_bytes() == primary_before


def test_renew_and_release_reclaim_orphaned_protocol_markers_safely(
    tmp_path: Path,
) -> None:
    port = CommunityLocalWriteLockPort()
    base_dir = tmp_path / "locks"
    lock = KGSingleWriterLock(base_dir=base_dir, write_lock_port=port)
    acquired = lock.acquire(
        board_id="board-renew-release-orphan",
        operation="rebuild",
        owner_id="owner-a",
        ttl_seconds=60,
    )
    assert acquired.acquired and acquired.owner_token
    recovery_path = (
        base_dir / "board-renew-release-orphan" / RECOVERY_LOCK_FILENAME
    )

    _write_protocol_recovery_marker(
        recovery_path,
        board_id="board-renew-release-orphan",
        owner_id="crashed-renewer",
    )
    assert lock.renew(
        board_id="board-renew-release-orphan",
        owner_token=str(acquired.owner_token),
        ttl_seconds=60,
    )
    assert not recovery_path.exists()

    _write_protocol_recovery_marker(
        recovery_path,
        board_id="board-renew-release-orphan",
        owner_id="crashed-releaser",
    )
    assert lock.release(
        board_id="board-renew-release-orphan",
        owner_token=str(acquired.owner_token),
    )
    assert not recovery_path.exists()


def test_protocol_marker_cleanup_does_not_unlink_foreign_replacement_token(
    tmp_path: Path,
) -> None:
    port = CommunityLocalWriteLockPort()
    board_dir = tmp_path / "locks" / "board-marker-cas"
    board_dir.mkdir(parents=True)
    recovery_path = board_dir / RECOVERY_LOCK_FILENAME
    token, _ = port._claim_single_writer_recovery_marker(  # noqa: SLF001
        recovery_path=recovery_path,
        board_id="board-marker-cas",
        operation="recover",
        owner_id="owner-a",
    )
    assert token
    replacement = json.loads(recovery_path.read_text(encoding="utf-8"))
    replacement["recovery_token"] = "foreign-token"
    recovery_path.write_text(json.dumps(replacement), encoding="utf-8")

    port._release_single_writer_recovery_marker(  # noqa: SLF001
        recovery_path,
        recovery_token=str(token),
    )

    assert json.loads(recovery_path.read_text(encoding="utf-8"))[
        "recovery_token"
    ] == "foreign-token"


def test_af29_s2_community_quarantine_adapter_preserves_manifest_and_scope(
    tmp_path: Path,
):
    store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path / "audit-store")
    graph_root = tmp_path / "graphs"
    graph_root.mkdir()
    graph_file = graph_root / "graph.lbug"
    graph_file.write_text("corrupt-bytes", encoding="utf-8")

    service = KGQuarantineService(
        base_storage_ref_hint=local_storage_ref(tmp_path / "kg-root"),
        scope_storage_refs=[local_storage_ref(graph_root)],
        artifact_store=store,
    )
    response = service.create(
        board_id="board-a",
        graph_type="board_graph",
        affected_storage_refs=[local_storage_ref(graph_file)],
        reason="corruption detected during reopen probe",
        correlation_ids=["corr-1"],
        kg_generation_id="kg-1",
    )

    assert response.files_moved == 1
    assert graph_file.exists() is False
    manifest_path = Path(response.manifest_ref)
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["board_id"] == "board-a"
    assert manifest["graph_type"] == "board_graph"
    assert manifest["reason_bucket"] == "corruption_detected"
    assert manifest["kg_generation_id"] == "kg-1"

    assert service.inspect(response.quarantine_id).files_moved == 1
    assert [m.quarantine_id for m in service.list_active()] == [response.quarantine_id]

    outside = tmp_path / "outside.lbug"
    outside.write_text("app-data", encoding="utf-8")
    with pytest.raises(QuarantineError) as exc_info:
        service.create(
            board_id="board-a",
            graph_type="board_graph",
            affected_storage_refs=[local_storage_ref(outside)],
            reason="manual purge",
            correlation_ids=[],
        )
    assert exc_info.value.code is QuarantineErrorCode.STORAGE_REF_OUT_OF_SCOPE
    assert outside.exists()
