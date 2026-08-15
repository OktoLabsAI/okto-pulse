from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path

import pytest

from okto_pulse.community.adapters.rebuild_audit_storage import (
    CommunityFileSystemRebuildAuditArtifactStore,
)
from okto_pulse.community.adapters.storage import CommunityFileSystemStorage
from okto_pulse.core.kg.interfaces.rebuild_audit_storage import RebuildAuditKey


@pytest.mark.asyncio
async def test_attachment_storage_purge_is_board_scoped_and_idempotent(
    tmp_path: Path,
) -> None:
    storage = CommunityFileSystemStorage(str(tmp_path / "uploads"))
    target_path = Path(await storage.save("board-target", "target.txt", b"target"))
    other_path = Path(await storage.save("board-other", "other.txt", b"other"))

    result = await storage.purge_board("board-target")

    assert result == {
        "board_id": "board-target",
        "objects_removed": 1,
        "directories_removed": 1,
        "verified_absent": True,
        "status": "purged",
    }
    assert not target_path.exists()
    assert other_path.read_bytes() == b"other"
    assert (await storage.purge_board("board-target"))["status"] == "not_found"
    with pytest.raises(RuntimeError, match="permanently erased"):
        await storage.save("board-target", "late.txt", b"late")

    with pytest.raises(ValueError, match="safe logical identifier"):
        await storage.purge_board("../board-other")
    assert other_path.exists()


@pytest.mark.asyncio
async def test_attachment_purge_fences_a_racing_save(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.community.adapters import storage as storage_module

    storage = CommunityFileSystemStorage(str(tmp_path / "uploads"))
    await storage.save("board-target", "existing.txt", b"existing")
    entered = threading.Event()
    finish = threading.Event()
    original_remove = storage_module.remove_contained_tree

    def _blocked_remove(*args, **kwargs):
        entered.set()
        assert finish.wait(timeout=5)
        return original_remove(*args, **kwargs)

    monkeypatch.setattr(storage_module, "remove_contained_tree", _blocked_remove)
    purge = asyncio.create_task(storage.purge_board("board-target"))
    assert await asyncio.to_thread(entered.wait, 2)
    late_save = asyncio.create_task(storage.save("board-target", "late.txt", b"late"))
    await asyncio.sleep(0.05)
    assert not late_save.done()

    finish.set()
    assert (await purge)["verified_absent"] is True
    with pytest.raises(RuntimeError, match="permanently erased"):
        await late_save
    assert not (storage.base_dir / "board-target").exists()


@pytest.mark.asyncio
async def test_attachment_storage_purge_never_follows_directory_symlink(
    tmp_path: Path,
) -> None:
    storage = CommunityFileSystemStorage(str(tmp_path / "uploads"))
    board_dir = storage.base_dir / "board-target"
    board_dir.mkdir(parents=True)
    (board_dir / "owned.txt").write_text("owned", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "must-survive.txt"
    outside_file.write_text("outside", encoding="utf-8")
    try:
        os.symlink(
            outside,
            board_dir / "linked-outside",
            target_is_directory=True,
        )
    except (OSError, NotImplementedError):
        pytest.skip("directory symlink creation is unavailable")

    result = await storage.purge_board("board-target")

    assert result["status"] == "purged"
    assert result["verified_absent"] is True
    assert outside_file.read_text(encoding="utf-8") == "outside"
    assert not board_dir.exists()


def _write(
    store: CommunityFileSystemRebuildAuditArtifactStore,
    key: RebuildAuditKey,
    payload: dict[str, object],
) -> RebuildAuditKey:
    store.write_json_atomic(key, payload)
    return key


def test_rebuild_artifact_purge_covers_partitioned_shared_and_quarantine_state(
    tmp_path: Path,
) -> None:
    store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path)
    target = "board-target"
    other = "board-other"

    target_keys = [
        _write(
            store,
            RebuildAuditKey(
                namespace="event_audit",
                board_id=target,
                artifact_id="event-target",
            ),
            {"board_id": target},
        ),
        _write(
            store,
            RebuildAuditKey(
                namespace="cognitive_pending",
                board_id=target,
                kg_generation_id="generation-target",
            ),
            {"board_id": target},
        ),
        _write(
            store,
            RebuildAuditKey(
                namespace="confirmation_audit",
                board_id=target,
                artifact_id="confirmation-target",
            ),
            {"expected_board_id": target},
        ),
        _write(
            store,
            RebuildAuditKey(
                namespace="rebuild_confirmation_receipt",
                board_id=target,
                artifact_id="receipt-target",
            ),
            {"board_id": target, "run_id": "run-target"},
        ),
        _write(
            store,
            RebuildAuditKey(
                namespace="generation_current",
                board_id=target,
                artifact_id="current",
            ),
            {"board_id": target},
        ),
        _write(
            store,
            RebuildAuditKey(
                namespace="generation_history",
                board_id=target,
                kg_generation_id="history-target",
            ),
            {"board_id": target},
        ),
        _write(
            store,
            RebuildAuditKey(
                namespace="candidate_decision",
                board_id=target,
                artifact_id="decision-target",
            ),
            {"board_id": target},
        ),
        _write(
            store,
            RebuildAuditKey(
                namespace="global_discovery_reindex",
                board_id=target,
                kg_generation_id="reindex-target",
            ),
            {"board_id": target},
        ),
    ]
    other_partitioned = _write(
        store,
        RebuildAuditKey(
            namespace="event_audit",
            board_id=other,
            artifact_id="event-other",
        ),
        {"board_id": other},
    )
    other_receipt = _write(
        store,
        RebuildAuditKey(
            namespace="rebuild_confirmation_receipt",
            board_id=other,
            artifact_id="receipt-other",
        ),
        {"board_id": other, "run_id": "run-other"},
    )

    shared_specs = [
        ("run_audit", target, "run-target", {"board_id": target}),
        (
            "source_manifest",
            "_global",
            "manifest-target",
            {"source": {"board_id": target}},
        ),
        (
            "confirmation_token",
            "_global",
            "confirmation-token-target",
            {"expected_board_id": target},
        ),
        (
            "rebuild_report",
            target,
            "report-target",
            {"summary": {"board_id": target}},
        ),
        (
            "rebaseline_audit",
            target,
            "rebaseline-target",
            {"records": [{"board_id": target}]},
        ),
        (
            "global_discovery_recovery",
            "_global",
            "recovery-target",
            {"board_ids": [target, other]},
        ),
        (
            "contingency",
            target,
            "contingency-target",
            {"board_id": target},
        ),
        (
            "stress_evidence",
            "_global",
            "stress-target",
            {"details": {"source_board_id": target}},
        ),
    ]
    shared_target_keys = [
        _write(
            store,
            RebuildAuditKey(
                namespace=namespace,  # type: ignore[arg-type]
                board_id=key_board_id,
                artifact_id=artifact_id,
            ),
            payload,
        )
        for namespace, key_board_id, artifact_id, payload in shared_specs
    ]
    other_shared = _write(
        store,
        RebuildAuditKey(
            namespace="source_manifest",
            board_id="_global",
            artifact_id="manifest-other",
        ),
        {"board_id": other},
    )

    target_quarantine = tmp_path / "quarantine" / "q_target"
    target_quarantine.mkdir(parents=True)
    (target_quarantine / "graph.lbug").write_bytes(b"graph")
    (target_quarantine / "manifest.json").write_text(
        json.dumps({"board_id": target, "quarantine_id": "q_target"}),
        encoding="utf-8",
    )
    other_quarantine = tmp_path / "quarantine" / "q_other"
    other_quarantine.mkdir()
    (other_quarantine / "manifest.json").write_text(
        json.dumps({"board_id": other, "quarantine_id": "q_other"}),
        encoding="utf-8",
    )
    global_quarantine = tmp_path / "quarantine" / "q_global"
    global_quarantine.mkdir()
    (global_quarantine / "discovery.lbug").write_bytes(b"global-copy")
    (global_quarantine / "manifest.json").write_text(
        json.dumps(
            {
                "board_id": "_global",
                "graph_type": "global_discovery",
                "quarantine_id": "q_global",
            }
        ),
        encoding="utf-8",
    )
    wal_only = tmp_path / "quarantine" / f"wal-only-{target}-failed-manifest"
    wal_only.mkdir()
    (wal_only / "graph.lbug.wal").write_bytes(b"target-wal")
    global_recovery = tmp_path / "rebuild" / "global_discovery_recovery" / "opaque.json"
    global_recovery.parent.mkdir(parents=True, exist_ok=True)
    global_recovery.write_text(
        json.dumps({"semantic_fingerprint": "opaque-cross-board-hash"}),
        encoding="utf-8",
    )

    result = store.purge_board_artifacts(target)

    assert result["status"] == "purged"
    assert result["verified_absent"] is True
    assert result["files_removed"] >= len(target_keys) + len(shared_target_keys)
    assert result["directories_removed"] >= 7
    for key in [*target_keys, *shared_target_keys]:
        assert not store.exists(key)
    assert store.exists(other_partitioned)
    assert store.exists(other_receipt)
    assert store.exists(other_shared)
    assert not target_quarantine.exists()
    assert other_quarantine.exists()
    assert not global_quarantine.exists()
    assert not wal_only.exists()
    assert not global_recovery.exists()
    assert store.purge_board_artifacts(target)["status"] == "not_found"


@pytest.mark.parametrize(
    ("crash_cut", "expected_active", "expected_histories"),
    (
        ("before_receipts", True, {"run-a.json", "run-b.json"}),
        ("between_histories", True, {"run-b.json"}),
        ("before_active", True, set()),
        ("after_active", False, set()),
    ),
)
def test_rebuild_artifact_purge_deletes_receipt_active_last(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_cut: str,
    expected_active: bool,
    expected_histories: set[str],
) -> None:
    from okto_pulse.community.adapters import rebuild_audit_storage as storage_module

    store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path)
    board_id = "board-target"
    ordinary_key = _write(
        store,
        RebuildAuditKey(
            namespace="event_audit",
            board_id=board_id,
            artifact_id="event-before-receipts",
        ),
        {"board_id": board_id},
    )
    for run_id in ("run-a", "run-b"):
        _write(
            store,
            RebuildAuditKey(
                namespace="rebuild_confirmation_receipt",
                board_id=board_id,
                artifact_id=run_id,
            ),
            {"board_id": board_id, "run_id": run_id},
        )
    active_key = _write(
        store,
        RebuildAuditKey(
            namespace="rebuild_confirmation_receipt",
            board_id=board_id,
            artifact_id="active",
        ),
        {"board_id": board_id, "run_id": "run-b"},
    )

    original_remove = storage_module.remove_contained_tree

    def _crash_at_boundary(path: Path, *, base_dir: Path) -> tuple[int, int]:
        name = Path(path).name
        if crash_cut == "before_receipts" and name == "run-a.json":
            raise RuntimeError("injected crash before receipt phase")
        if crash_cut == "between_histories" and name == "run-b.json":
            raise RuntimeError("injected crash between receipt histories")
        if crash_cut == "before_active" and name == "active.json":
            raise RuntimeError("injected crash before active sentinel")
        result = original_remove(path, base_dir=base_dir)
        if crash_cut == "after_active" and name == "active.json":
            raise RuntimeError("injected crash after active sentinel")
        return result

    monkeypatch.setattr(storage_module, "remove_contained_tree", _crash_at_boundary)
    with pytest.raises(RuntimeError, match="injected crash"):
        store.purge_board_artifacts(board_id)

    # The receipt phase starts only after every other board artifact is gone.
    assert not store.exists(ordinary_key)
    assert store.exists(active_key) is expected_active
    receipt_dir = tmp_path / "rebuild" / "audit" / "confirmation_receipts" / board_id
    remaining_histories = {
        path.name for path in receipt_dir.glob("*.json") if path.name != "active.json"
    }
    assert remaining_histories == expected_histories


def test_rebuild_artifact_purge_removes_all_other_targets_before_receipt_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.community.adapters import rebuild_audit_storage as storage_module

    store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path)
    board_id = "board-target"
    event_key = _write(
        store,
        RebuildAuditKey(
            namespace="event_audit",
            board_id=board_id,
            artifact_id="event",
        ),
        {"board_id": board_id},
    )
    history_key = _write(
        store,
        RebuildAuditKey(
            namespace="rebuild_confirmation_receipt",
            board_id=board_id,
            artifact_id="run-history",
        ),
        {"board_id": board_id, "run_id": "run-history"},
    )
    active_key = _write(
        store,
        RebuildAuditKey(
            namespace="rebuild_confirmation_receipt",
            board_id=board_id,
            artifact_id="active",
        ),
        {"board_id": board_id, "run_id": "run-history"},
    )
    original_remove = storage_module.remove_contained_tree
    observed: list[str] = []
    event_path = Path(store.reference(event_key))

    def _observe_order(path: Path, *, base_dir: Path) -> tuple[int, int]:
        name = Path(path).name
        if name in {"run-history.json", "active.json"}:
            observed.append(name)
            assert not event_path.exists()
        return original_remove(path, base_dir=base_dir)

    monkeypatch.setattr(storage_module, "remove_contained_tree", _observe_order)
    assert store.purge_board_artifacts(board_id)["status"] == "purged"

    assert observed == ["run-history.json", "active.json"]
    assert not store.exists(event_key)
    assert not store.exists(history_key)
    assert not store.exists(active_key)


@pytest.mark.parametrize("alias_kind", ("root", "active", "history"))
def test_rebuild_artifact_purge_preflights_receipt_symlinks_before_mutation(
    tmp_path: Path,
    alias_kind: str,
) -> None:
    base_dir = tmp_path / "kg"
    store = CommunityFileSystemRebuildAuditArtifactStore(base_dir)
    board_id = "board-target"
    ordinary_key = _write(
        store,
        RebuildAuditKey(
            namespace="event_audit",
            board_id=board_id,
            artifact_id="must-survive-failed-preflight",
        ),
        {"board_id": board_id},
    )
    receipt_dir = base_dir / "rebuild" / "audit" / "confirmation_receipts" / board_id
    outside = tmp_path / f"outside-{alias_kind}"
    if alias_kind == "root":
        outside.mkdir()
        receipt_dir.parent.mkdir(parents=True, exist_ok=True)
        link_path = receipt_dir
        target_is_directory = True
    else:
        receipt_dir.mkdir(parents=True)
        outside.write_text("{}", encoding="utf-8")
        link_path = receipt_dir / (
            "active.json" if alias_kind == "active" else "run-history.json"
        )
        target_is_directory = False
    try:
        os.symlink(outside, link_path, target_is_directory=target_is_directory)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(RuntimeError, match="rebuild confirmation receipt"):
        store.purge_board_artifacts(board_id)

    assert store.exists(ordinary_key)
    assert link_path.exists()
    assert outside.exists()


@pytest.mark.parametrize("artifact_name", ("active.json", "run-history.json"))
def test_rebuild_artifact_purge_preflights_receipt_hardlinks_before_mutation(
    tmp_path: Path,
    artifact_name: str,
) -> None:
    store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path)
    board_id = "board-target"
    ordinary_key = _write(
        store,
        RebuildAuditKey(
            namespace="event_audit",
            board_id=board_id,
            artifact_id="must-survive-hardlink-preflight",
        ),
        {"board_id": board_id},
    )
    receipt_dir = tmp_path / "rebuild" / "audit" / "confirmation_receipts" / board_id
    receipt_dir.mkdir(parents=True)
    outside = tmp_path / f"outside-{artifact_name}"
    outside.write_text("{}", encoding="utf-8")
    alias = receipt_dir / artifact_name
    try:
        os.link(outside, alias)
    except (OSError, NotImplementedError):
        pytest.skip("hardlink creation is unavailable")

    with pytest.raises(RuntimeError, match="private regular JSON"):
        store.purge_board_artifacts(board_id)

    assert store.exists(ordinary_key)
    assert alias.exists()
    assert outside.exists()


def test_rebuild_artifact_purge_preflights_unreadable_shared_state(
    tmp_path: Path,
) -> None:
    store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path)
    target_key = _write(
        store,
        RebuildAuditKey(
            namespace="event_audit",
            board_id="board-target",
            artifact_id="event-target",
        ),
        {"board_id": "board-target"},
    )
    malformed = tmp_path / "rebuild" / "manifests" / "malformed.json"
    malformed.parent.mkdir(parents=True)
    malformed.write_text("{", encoding="utf-8")

    with pytest.raises(RuntimeError, match="board scope is unreadable"):
        store.purge_board_artifacts("board-target")

    assert store.exists(target_key)
    malformed.write_text(
        json.dumps({"board_id": "board-other"}),
        encoding="utf-8",
    )
    assert store.purge_board_artifacts("board-target")["status"] == "purged"


def test_rebuild_artifact_purge_rejects_linked_parent_without_escape(
    tmp_path: Path,
) -> None:
    store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path / "kg")
    outside = tmp_path / "outside"
    target_outside = outside / "board-target"
    target_outside.mkdir(parents=True)
    outside_file = target_outside / "event.json"
    outside_file.write_text("outside", encoding="utf-8")
    events = tmp_path / "kg" / "rebuild" / "audit" / "events"
    events.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(outside, events, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlink creation is unavailable")

    with pytest.raises(ValueError, match="linked parent"):
        store.purge_board_artifacts("board-target")

    assert outside_file.read_text(encoding="utf-8") == "outside"
