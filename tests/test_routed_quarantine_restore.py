"""Discriminating contracts for backend-neutral quarantine restore routing."""

from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from okto_pulse.core.kg.interfaces.quarantine_restore import (
    QuarantineRestoreError,
    RestoreFileEntry,
    RestorePlan,
    RestoreReport,
)

import okto_pulse.community.adapters.routed_quarantine_restore as routed_module
from okto_pulse.community.adapters.composition import (
    _apply_quarantine_restore,
    build_community_routed_quarantine_restore,
)
from okto_pulse.community.adapters.grafx_board_storage import (
    _authenticated_manifest,
    _canonical_sha256,
)
from okto_pulse.community.adapters.graph_backend_binding import (
    CommunityGraphBackendBindingStore,
)
from okto_pulse.community.adapters.graph_route_resolver import (
    CommunityGraphRouteResolver,
)
from okto_pulse.community.adapters.kg_wal_recovery import wal_only_quarantine
from okto_pulse.community.adapters.routed_quarantine_restore import (
    CommunityGrafxSnapshotRestoreFactory,
    CommunityRoutedQuarantineRestore,
)

BOARD_ID = "board-routed-restore"
PAGE_SIZE = 8192
LEGACY_BOARD_ID = "11111111-1111-4111-8111-111111111111"
OTHER_LEGACY_BOARD_ID = "22222222-2222-4222-8222-222222222222"


class _FakeGrafxDatabase:
    def __init__(self, path: Path) -> None:
        self.path = str(path)
        self.identity = SimpleNamespace(page_size=PAGE_SIZE)


class _RecordingRestore:
    def __init__(self, *, board_id: str, board_dir: Path) -> None:
        self.board_id = board_id
        self.board_dir = board_dir
        self.plan_calls: list[str] = []
        self.apply_calls: list[str] = []
        self.compensation_calls: list[tuple[str, str]] = []
        self.discard_calls: list[str] = []

    def plan(self, quarantine_id: str) -> RestorePlan:
        self.plan_calls.append(quarantine_id)
        return RestorePlan(
            quarantine_id=quarantine_id,
            board_id=self.board_id,
            board_dir=str(self.board_dir),
            manifest_format="test",
            files=(
                RestoreFileEntry(
                    name="payload",
                    source_path="source",
                    destination_path="destination",
                    size_bytes=1,
                    conflict=False,
                ),
            ),
            total_bytes=1,
        )

    def apply(self, quarantine_id: str) -> RestoreReport:
        self.apply_calls.append(quarantine_id)
        return RestoreReport(
            quarantine_id=quarantine_id,
            board_id=self.board_id,
            applied=True,
            backup_quarantine_id="backup",
            restored_files=("payload",),
            open_validated=True,
        )

    def apply_rebuild_compensation(
        self,
        quarantine_id: str,
        *,
        expected_board_id: str,
        run_id: str,
        owner_token: str | None,
    ) -> RestoreReport:
        assert owner_token
        self.compensation_calls.append((quarantine_id, run_id))
        return self.apply(quarantine_id)

    def discard_rebuild_candidate(
        self,
        *,
        expected_board_id: str,
        run_id: str,
        owner_token: str | None,
    ) -> dict[str, object]:
        assert expected_board_id == self.board_id
        assert owner_token
        self.discard_calls.append(run_id)
        return {"status": "discarded", "live_absent": True}


def _resolver(
    root: Path,
    *,
    opened: list[Path] | None = None,
) -> tuple[CommunityGraphBackendBindingStore, CommunityGraphRouteResolver]:
    store = CommunityGraphBackendBindingStore(root)

    def open_database(path: Path):
        assert opened is not None
        opened.append(path)
        raise AssertionError("route inspection must not open Grafx")

    return store, CommunityGraphRouteResolver(
        store,
        board_backend="ladybug",
        global_backend="ladybug",
        grafx_page_size=PAGE_SIZE,
        open_grafx_database=open_database if opened is not None else None,
    )


def _bind_ladybug(store: CommunityGraphBackendBindingStore) -> Path:
    path = store.board_ladybug_path(BOARD_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"ladybug")
    store.initialize_board_binding(
        board_id=BOARD_ID,
        backend="ladybug",
        generation="generation-1",
        physical_path=path,
    )
    return path


def _bind_grafx(store: CommunityGraphBackendBindingStore) -> Path:
    path = store.board_grafx_path(BOARD_ID, "generation-1")
    path.mkdir(parents=True, exist_ok=True)
    (path / "grafx.meta").write_bytes(b"grafx")
    store.initialize_board_binding(
        board_id=BOARD_ID,
        backend="grafx",
        generation="generation-1",
        physical_path=path,
        page_size=PAGE_SIZE,
        database=_FakeGrafxDatabase(path),
    )
    return path


def _ladybug_quarantine(root: Path, quarantine_id: str = "q_ladybug") -> Path:
    directory = root / "quarantine" / quarantine_id
    directory.mkdir(parents=True)
    (directory / "graph.lbug").write_bytes(b"snapshot")
    manifest = {
        "quarantine_id": quarantine_id,
        "board_id": BOARD_ID,
        "graph_type": "board_graph",
        "reason": "test",
        "reason_bucket": "test",
        "correlation_ids": [],
        "affected_paths_relative": ["graph.lbug"],
        "kg_generation_id": None,
        "software_version": "test",
        "quarantined_at": "2026-08-28T00:00:00+00:00",
        "retention_until": "2026-09-28T00:00:00+00:00",
        "files_moved": 1,
    }
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return directory


def _grafx_wal_quarantine(
    root: Path,
    path: Path,
    quarantine_id: str = "grafx-wal-route",
) -> Path:
    directory = root / "quarantine" / quarantine_id
    payload = directory / "payload" / "wal"
    payload.mkdir(parents=True)
    (payload / "000000000001.wal").write_bytes(b"wal")
    manifest = {
        "format": "pulse_grafx_quarantine/1",
        "kind": "grafx_wal_only",
        "quarantine_id": quarantine_id,
        "board_id": BOARD_ID,
        "database_path": str(path),
        "main_untouched": True,
        "complete": True,
        "phase": "recovered",
        "files": [
            {
                "relative_path": "wal/000000000001.wal",
                "size_bytes": 3,
                "sha256": hashlib.sha256(b"wal").hexdigest(),
            }
        ],
    }
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return directory


def _grafx_directory_quarantine(
    root: Path,
    path: Path,
    binding_sha256: str,
    quarantine_id: str = "grafx-directory-route",
) -> Path:
    directory = root / "quarantine" / quarantine_id
    payload = directory / "payload" / "database"
    payload.mkdir(parents=True)
    (payload / "grafx.meta").write_bytes(b"grafx")
    files = [{"relative_path": "grafx.meta", "size_bytes": 5, "sha256": "0" * 64}]
    manifest = {
        "format": "pulse_grafx_quarantine/1",
        "kind": "grafx_board_directory",
        "quarantine_id": quarantine_id,
        "board_id": BOARD_ID,
        "database_path": str(path),
        "generation": "generation-1",
        "binding_sha256": binding_sha256,
        "payload_relative": "payload/database",
        "directories": [],
        "files": files,
        "inventory_sha256": _canonical_sha256({"directories": [], "files": files}),
        "complete": True,
        "phase": "captured",
    }
    (directory / "manifest.json").write_text(
        json.dumps(_authenticated_manifest(manifest)), encoding="utf-8"
    )
    return directory


def _produced_ladybug_wal_quarantine(root: Path) -> tuple[Path, Path, str]:
    graph_path = root / "boards" / BOARD_ID / "graph.lbug"
    graph_path.parent.mkdir(parents=True)
    graph_path.write_bytes(b"main")
    graph_path.with_name("graph.lbug.wal").write_bytes(b"wal-bytes")
    graph_path.with_name("graph.lbug.shadow").write_bytes(b"shadow-bytes")

    result = wal_only_quarantine(
        BOARD_ID,
        "routed-restore-ratchet",
        graph_path=graph_path,
    )

    assert result.ok is True
    assert result.quarantine_id is not None
    return graph_path, root / "quarantine" / result.quarantine_id, result.quarantine_id


def _legacy_interrupted_quarantine(
    root: Path,
    *,
    quarantine_board_id: str = LEGACY_BOARD_ID,
    manifest_board_id: str = LEGACY_BOARD_ID,
    declared_files: tuple[str, ...] = ("graph.lbug.shadow",),
    payload_files: tuple[str, ...] = ("graph.lbug.shadow",),
) -> tuple[Path, str]:
    graph_path = root / "boards" / manifest_board_id / "graph.lbug"
    quarantine_id = f"interrupted-checkpoint-{quarantine_board_id}-20260828T000000"
    directory = root / "quarantine" / quarantine_id
    directory.mkdir(parents=True)
    for name in payload_files:
        (directory / name).write_bytes(f"payload:{name}".encode())
    (directory / "manifest.txt").write_text(
        "Sidecars orfaos de checkpoint interrompido movidos automaticamente "
        f"para destravar a abertura de {graph_path}. "
        "Main file preservado no lugar. "
        f"Arquivos: {', '.join(declared_files)}.",
        encoding="utf-8",
    )
    return graph_path, quarantine_id


def _routed(
    root: Path,
    resolver: CommunityGraphRouteResolver,
    ladybug: _RecordingRestore,
    grafx: _RecordingRestore | None = None,
    *,
    factory_calls: list[object] | None = None,
) -> CommunityRoutedQuarantineRestore:
    def factory(snapshot):
        if factory_calls is not None:
            factory_calls.append(snapshot)
        assert grafx is not None
        return grafx

    return CommunityRoutedQuarantineRestore(
        resolver,
        quarantine_root=root / "quarantine",
        ladybug=ladybug,
        grafx_factory=factory if grafx is not None else None,
    )


def test_missing_binding_accepts_only_strict_ladybug_without_opening(
    tmp_path: Path,
) -> None:
    _ladybug_quarantine(tmp_path)
    store, resolver = _resolver(tmp_path)
    ladybug = _RecordingRestore(
        board_id=BOARD_ID,
        board_dir=store.board_ladybug_path(BOARD_ID).parent,
    )

    plan = _routed(tmp_path, resolver, ladybug).plan("q_ladybug")

    assert plan.board_id == BOARD_ID
    assert ladybug.plan_calls == ["q_ladybug"]
    assert not (tmp_path / "boards").exists()


def test_real_ladybug_wal_producer_routes_with_authenticated_inventory(
    tmp_path: Path,
) -> None:
    graph_path, directory, quarantine_id = _produced_ladybug_wal_quarantine(tmp_path)
    _store, resolver = _resolver(tmp_path)
    adapter = build_community_routed_quarantine_restore(
        kg_base_dir=str(tmp_path),
        data_dir=str(tmp_path),
        graph_route_resolver=resolver,
        grafx_restore_factory=None,
    )

    plan = adapter.plan(quarantine_id)

    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["kind"] == "kg_wal_only_quarantine"
    assert manifest["files"] == ["graph.lbug.wal", "graph.lbug.shadow"]
    assert all(type(item["size"]) is int for item in manifest["planned_files"])
    assert all(len(item["sha256"]) == 64 for item in manifest["planned_files"])
    assert plan.board_id == BOARD_ID
    assert Path(plan.board_dir) == graph_path.parent
    assert [entry.name for entry in plan.files] == [
        "graph.lbug.shadow",
        "graph.lbug.wal",
    ]


@pytest.mark.parametrize(
    ("declared_files", "payload_files"),
    [
        (("graph.lbug.shadow",), ("graph.lbug",)),
        (("graph.lbug",), ("graph.lbug",)),
        (("graph.lbug.wal",), ("graph.lbug.wal",)),
    ],
)
def test_legacy_interrupted_checkpoint_never_routes_main_or_primary_wal(
    tmp_path: Path,
    declared_files: tuple[str, ...],
    payload_files: tuple[str, ...],
) -> None:
    graph_path, quarantine_id = _legacy_interrupted_quarantine(
        tmp_path,
        declared_files=declared_files,
        payload_files=payload_files,
    )
    graph_path.parent.mkdir(parents=True)
    graph_path.write_bytes(b"live-board-must-survive")
    _store, resolver = _resolver(tmp_path)
    ladybug = _RecordingRestore(board_id=LEGACY_BOARD_ID, board_dir=graph_path.parent)

    with pytest.raises(QuarantineRestoreError):
        _routed(tmp_path, resolver, ladybug).apply(quarantine_id)

    assert ladybug.apply_calls == []
    assert graph_path.read_bytes() == b"live-board-must-survive"


def test_legacy_interrupted_checkpoint_refuses_conflicting_board_identities(
    tmp_path: Path,
) -> None:
    graph_path, quarantine_id = _legacy_interrupted_quarantine(
        tmp_path,
        quarantine_board_id=LEGACY_BOARD_ID,
        manifest_board_id=OTHER_LEGACY_BOARD_ID,
    )
    graph_path.parent.mkdir(parents=True)
    graph_path.write_bytes(b"other-live-board-must-survive")
    _store, resolver = _resolver(tmp_path)
    ladybug = _RecordingRestore(board_id=LEGACY_BOARD_ID, board_dir=graph_path.parent)

    with pytest.raises(QuarantineRestoreError, match="inconsistent board identity"):
        _routed(tmp_path, resolver, ladybug).apply(quarantine_id)

    assert ladybug.apply_calls == []
    assert graph_path.read_bytes() == b"other-live-board-must-survive"


def test_legacy_interrupted_checkpoint_routes_exact_producer_sidecars(
    tmp_path: Path,
) -> None:
    graph_path, quarantine_id = _legacy_interrupted_quarantine(
        tmp_path,
        declared_files=("graph.lbug.shadow", "graph.lbug.wal.checkpoint"),
        payload_files=("graph.lbug.shadow", "graph.lbug.wal.checkpoint"),
    )
    _store, resolver = _resolver(tmp_path)
    ladybug = _RecordingRestore(board_id=LEGACY_BOARD_ID, board_dir=graph_path.parent)

    plan = _routed(tmp_path, resolver, ladybug).plan(quarantine_id)

    assert plan.board_id == LEGACY_BOARD_ID
    assert ladybug.plan_calls == [quarantine_id]


@pytest.mark.parametrize(
    "mutation",
    [
        "quarantine_id",
        "main_untouched",
        "error",
        "graph_path",
        "main_file",
        "planned_size",
        "planned_digest",
        "moved_order",
        "payload_tamper",
        "namespace_extra",
        "extra_field",
    ],
)
def test_real_ladybug_wal_schema_tamper_fails_before_provider(
    tmp_path: Path,
    mutation: str,
) -> None:
    graph_path, directory, quarantine_id = _produced_ladybug_wal_quarantine(tmp_path)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if mutation == "quarantine_id":
        manifest["quarantine_id"] = "different"
    elif mutation == "main_untouched":
        manifest["main_untouched"] = False
    elif mutation == "error":
        manifest["error"] = "partial move"
    elif mutation == "graph_path":
        manifest["graph_path"] = str(
            tmp_path / "alternate" / "boards" / BOARD_ID / "graph.lbug"
        )
    elif mutation == "main_file":
        manifest["main_file"] = "graph.lbug.wal"
    elif mutation == "planned_size":
        manifest["planned_files"][0]["size"] += 1
    elif mutation == "planned_digest":
        manifest["planned_files"][0]["sha256"] = "0" * 64
    elif mutation == "moved_order":
        manifest["files"] = list(reversed(manifest["files"]))
    elif mutation == "payload_tamper":
        (directory / manifest["files"][0]).write_bytes(b"changed")
    elif mutation == "namespace_extra":
        (directory / "graph.lbug").write_bytes(b"must-never-be-restored")
    elif mutation == "extra_field":
        manifest["unknown"] = "not-canonical"
    else:  # pragma: no cover - parametrization is closed above
        raise AssertionError(mutation)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    _store, resolver = _resolver(tmp_path)
    ladybug = _RecordingRestore(board_id=BOARD_ID, board_dir=graph_path.parent)

    with pytest.raises(QuarantineRestoreError):
        _routed(tmp_path, resolver, ladybug).plan(quarantine_id)

    assert ladybug.plan_calls == []


def test_unproven_old_ladybug_wal_kind_is_not_a_legacy_escape(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "quarantine" / "q_old_kind"
    directory.mkdir(parents=True)
    (directory / "graph.lbug").write_bytes(b"unverified-main")
    (directory / "manifest.json").write_text(
        json.dumps({"kind": "kg_wal_quarantine", "board_id": BOARD_ID}),
        encoding="utf-8",
    )
    store, resolver = _resolver(tmp_path)
    ladybug = _RecordingRestore(
        board_id=BOARD_ID,
        board_dir=store.board_ladybug_path(BOARD_ID).parent,
    )

    with pytest.raises(QuarantineRestoreError, match="kind is unsupported"):
        _routed(tmp_path, resolver, ladybug).plan("q_old_kind")

    assert ladybug.plan_calls == []


def test_missing_binding_refuses_grafx_before_factory_or_open(tmp_path: Path) -> None:
    path = tmp_path / "boards" / BOARD_ID / "grafx" / "generation-1"
    _grafx_wal_quarantine(tmp_path, path)
    store, resolver = _resolver(tmp_path)
    ladybug = _RecordingRestore(board_id=BOARD_ID, board_dir=path.parent)
    grafx = _RecordingRestore(board_id=BOARD_ID, board_dir=path)
    factory_calls: list[object] = []

    with pytest.raises(QuarantineRestoreError, match="no persisted Board binding"):
        _routed(
            tmp_path,
            resolver,
            ladybug,
            grafx,
            factory_calls=factory_calls,
        ).plan("grafx-wal-route")

    assert factory_calls == []
    assert not store.board_grafx_path(BOARD_ID, "generation-1").exists()


@pytest.mark.parametrize("bound_backend", ["ladybug", "grafx"])
def test_persisted_binding_is_authoritative_and_mismatch_never_falls_back(
    tmp_path: Path,
    bound_backend: str,
) -> None:
    store, resolver = _resolver(tmp_path)
    if bound_backend == "ladybug":
        bound_path = _bind_ladybug(store)
        quarantine_id = "grafx-wal-route"
        _grafx_wal_quarantine(
            tmp_path,
            tmp_path / "boards" / BOARD_ID / "grafx" / "generation-1",
        )
    else:
        bound_path = _bind_grafx(store)
        quarantine_id = "q_ladybug"
        _ladybug_quarantine(tmp_path)
    ladybug = _RecordingRestore(board_id=BOARD_ID, board_dir=bound_path.parent)
    grafx = _RecordingRestore(board_id=BOARD_ID, board_dir=bound_path)

    with pytest.raises(QuarantineRestoreError, match="conflicts"):
        _routed(tmp_path, resolver, ladybug, grafx).plan(quarantine_id)

    assert ladybug.plan_calls == []
    assert grafx.plan_calls == []


def test_grafx_wal_and_directory_are_fixed_to_exact_snapshot(tmp_path: Path) -> None:
    opened: list[Path] = []
    store, resolver = _resolver(tmp_path, opened=opened)
    path = _bind_grafx(store)
    snapshot = resolver.inspect_board_route(BOARD_ID)
    _grafx_wal_quarantine(tmp_path, path)
    _grafx_directory_quarantine(tmp_path, path, snapshot.binding_sha256)
    ladybug = _RecordingRestore(board_id=BOARD_ID, board_dir=path.parent)
    grafx = _RecordingRestore(board_id=BOARD_ID, board_dir=path)
    factory_calls: list[object] = []
    adapter = _routed(
        tmp_path,
        resolver,
        ladybug,
        grafx,
        factory_calls=factory_calls,
    )

    assert adapter.plan("grafx-wal-route").board_dir == str(path)
    assert adapter.plan("grafx-directory-route").board_dir == str(path)
    assert factory_calls == [snapshot, snapshot]
    assert ladybug.plan_calls == []
    assert opened == []


def test_concrete_grafx_factory_pins_dry_run_to_snapshot_without_open(
    tmp_path: Path,
) -> None:
    store, resolver = _resolver(tmp_path)
    path = _bind_grafx(store)
    _grafx_wal_quarantine(tmp_path, path)
    opened: list[tuple[object, Path]] = []
    factory = CommunityGrafxSnapshotRestoreFactory(
        resolver,
        quarantine_root=tmp_path / "quarantine",
        open_database=lambda snapshot, candidate: opened.append((snapshot, candidate)),
        close_board=lambda _board_id: None,
        board_is_locked=lambda _board_id: False,
        revalidate_fence=lambda _board_id, _phase: None,
        mutation_guard=lambda _board_id: nullcontext(),
    )
    ladybug = _RecordingRestore(board_id=BOARD_ID, board_dir=path.parent)
    adapter = CommunityRoutedQuarantineRestore(
        resolver,
        quarantine_root=tmp_path / "quarantine",
        ladybug=ladybug,
        grafx_factory=factory,
    )

    plan = adapter.plan("grafx-wal-route")

    assert plan.board_dir == str(path)
    assert [entry.name for entry in plan.files] == ["wal/000000000001.wal"]
    assert opened == []
    assert ladybug.plan_calls == []


def test_grafx_generation_binding_and_path_mismatch_fail_before_factory(
    tmp_path: Path,
) -> None:
    store, resolver = _resolver(tmp_path)
    path = _bind_grafx(store)
    snapshot = resolver.inspect_board_route(BOARD_ID)
    directory = _grafx_directory_quarantine(
        tmp_path,
        path,
        snapshot.binding_sha256,
    )
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    ladybug = _RecordingRestore(board_id=BOARD_ID, board_dir=path.parent)
    grafx = _RecordingRestore(board_id=BOARD_ID, board_dir=path)
    factory_calls: list[object] = []
    adapter = _routed(
        tmp_path,
        resolver,
        ladybug,
        grafx,
        factory_calls=factory_calls,
    )

    manifest["generation"] = "generation-poison"
    manifest_path.write_text(
        json.dumps(_authenticated_manifest(manifest)), encoding="utf-8"
    )
    with pytest.raises(QuarantineRestoreError, match="generation or binding"):
        adapter.plan("grafx-directory-route")
    manifest["generation"] = "generation-1"
    manifest["database_path"] = str(path.parent / "alternate")
    manifest_path.write_text(
        json.dumps(_authenticated_manifest(manifest)), encoding="utf-8"
    )
    with pytest.raises(QuarantineRestoreError, match="path conflicts"):
        adapter.plan("grafx-directory-route")

    assert factory_calls == []


def test_grafx_directory_tamper_and_duplicate_inventory_fail_before_factory(
    tmp_path: Path,
) -> None:
    store, resolver = _resolver(tmp_path)
    path = _bind_grafx(store)
    snapshot = resolver.inspect_board_route(BOARD_ID)
    directory = _grafx_directory_quarantine(
        tmp_path,
        path,
        snapshot.binding_sha256,
    )
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    ladybug = _RecordingRestore(board_id=BOARD_ID, board_dir=path.parent)
    grafx = _RecordingRestore(board_id=BOARD_ID, board_dir=path)
    factory_calls: list[object] = []
    adapter = _routed(
        tmp_path,
        resolver,
        ladybug,
        grafx,
        factory_calls=factory_calls,
    )

    manifest["reason"] = "tampered-without-authentication"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(QuarantineRestoreError, match="authentication failed"):
        adapter.plan("grafx-directory-route")

    manifest.pop("reason")
    manifest["files"].append(dict(manifest["files"][0]))
    manifest["inventory_sha256"] = _canonical_sha256(
        {"directories": manifest["directories"], "files": manifest["files"]}
    )
    manifest_path.write_text(
        json.dumps(_authenticated_manifest(manifest)), encoding="utf-8"
    )
    with pytest.raises(QuarantineRestoreError, match="unsafe"):
        adapter.plan("grafx-directory-route")

    assert factory_calls == []


def test_apply_reclassifies_and_reinspects_instead_of_reusing_dry_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _ladybug_quarantine(tmp_path)
    store, resolver = _resolver(tmp_path)
    ladybug_path = _bind_ladybug(store)
    inspections: list[str] = []
    inspect_route = resolver.inspect_board_route

    def inspect(board_id: str):
        inspections.append(board_id)
        return inspect_route(board_id)

    monkeypatch.setattr(resolver, "inspect_board_route", inspect)
    ladybug = _RecordingRestore(board_id=BOARD_ID, board_dir=ladybug_path.parent)
    adapter = _routed(tmp_path, resolver, ladybug)
    adapter.plan("q_ladybug")

    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    manifest.update(
        {
            "format": "pulse_grafx_quarantine/1",
            "kind": "grafx_wal_only",
            "database_path": str(
                tmp_path / "boards" / BOARD_ID / "grafx" / "generation-1"
            ),
            "main_untouched": True,
            "complete": True,
            "files": [
                {
                    "relative_path": "wal/000000000001.wal",
                    "size_bytes": 1,
                    "sha256": "0" * 64,
                }
            ],
        }
    )
    (directory / "payload").mkdir()
    (directory / "graph.lbug").unlink()
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(QuarantineRestoreError, match="conflicts"):
        adapter.apply("q_ladybug")
    assert ladybug.apply_calls == []
    assert inspections == [BOARD_ID, BOARD_ID]


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (
            lambda text: text.replace('"board_id"', '"board_id"', 1).replace(
                "{", '{"board_id":"duplicate",', 1
            ),
            "duplicate",
        ),
        (
            lambda text: text.replace(
                '"graph_type": "board_graph",', '"manifest_sha256": "' + "0" * 64 + '",'
            ),
            "Grafx markers",
        ),
        (
            lambda text: json.dumps(
                {
                    **json.loads(text),
                    "format": "pulse_grafx_quarantine/99",
                    "kind": "grafx_future_snapshot",
                }
            ),
            "unknown Grafx markers",
        ),
    ],
)
def test_duplicate_or_stripped_mixed_markers_fail_before_provider(
    tmp_path: Path,
    mutation,
    reason: str,
) -> None:
    directory = _ladybug_quarantine(tmp_path)
    manifest_path = directory / "manifest.json"
    manifest_path.write_text(
        mutation(manifest_path.read_text(encoding="utf-8")), encoding="utf-8"
    )
    store, resolver = _resolver(tmp_path)
    ladybug = _RecordingRestore(
        board_id=BOARD_ID,
        board_dir=store.board_ladybug_path(BOARD_ID).parent,
    )

    with pytest.raises(QuarantineRestoreError, match=reason):
        _routed(tmp_path, resolver, ladybug).plan("q_ladybug")
    assert ladybug.plan_calls == []


def test_ladybug_manifest_with_database_path_is_rejected_as_hybrid(
    tmp_path: Path,
) -> None:
    directory = _ladybug_quarantine(tmp_path)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["database_path"] = str(
        tmp_path / "boards" / BOARD_ID / "grafx" / "generation-1"
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    store, resolver = _resolver(tmp_path)
    ladybug = _RecordingRestore(
        board_id=BOARD_ID,
        board_dir=store.board_ladybug_path(BOARD_ID).parent,
    )

    with pytest.raises(QuarantineRestoreError, match="Grafx markers"):
        _routed(tmp_path, resolver, ladybug).plan("q_ladybug")

    assert ladybug.plan_calls == []


def test_authenticated_grafx_manifest_with_ladybug_markers_is_rejected(
    tmp_path: Path,
) -> None:
    store, resolver = _resolver(tmp_path)
    path = _bind_grafx(store)
    snapshot = resolver.inspect_board_route(BOARD_ID)
    directory = _grafx_directory_quarantine(
        tmp_path,
        path,
        snapshot.binding_sha256,
    )
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        {
            "graph_path": str(path.parent / "graph.lbug"),
            "main_file": "graph.lbug",
            "planned_files": [],
        }
    )
    manifest_path.write_text(
        json.dumps(_authenticated_manifest(manifest)),
        encoding="utf-8",
    )
    ladybug = _RecordingRestore(board_id=BOARD_ID, board_dir=path.parent)
    grafx = _RecordingRestore(board_id=BOARD_ID, board_dir=path)
    factory_calls: list[object] = []

    with pytest.raises(QuarantineRestoreError, match="mixes grafx and Ladybug"):
        _routed(
            tmp_path,
            resolver,
            ladybug,
            grafx,
            factory_calls=factory_calls,
        ).plan("grafx-directory-route")

    assert factory_calls == []
    assert grafx.plan_calls == []


def test_oversize_manifest_and_alternate_payload_fail_before_provider(
    tmp_path: Path,
) -> None:
    directory = _ladybug_quarantine(tmp_path)
    store, resolver = _resolver(tmp_path)
    ladybug = _RecordingRestore(
        board_id=BOARD_ID,
        board_dir=store.board_ladybug_path(BOARD_ID).parent,
    )
    adapter = _routed(tmp_path, resolver, ladybug)
    (directory / "manifest.json").write_bytes(b"{" + b" " * (1024 * 1024))
    with pytest.raises(QuarantineRestoreError, match="unreadable"):
        adapter.plan("q_ladybug")

    directory = _ladybug_quarantine(tmp_path, "q_poison")
    (directory / "payload").mkdir()
    with pytest.raises(QuarantineRestoreError, match="payload does not match"):
        adapter.plan("q_poison")
    assert ladybug.plan_calls == []


def test_alias_is_rejected_before_manifest_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _ladybug_quarantine(tmp_path)
    store, resolver = _resolver(tmp_path)
    ladybug = _RecordingRestore(
        board_id=BOARD_ID,
        board_dir=store.board_ladybug_path(BOARD_ID).parent,
    )
    real_probe = routed_module.is_filesystem_alias
    monkeypatch.setattr(
        routed_module,
        "is_filesystem_alias",
        lambda path: path == directory / "manifest.json" or real_probe(path),
    )

    with pytest.raises(QuarantineRestoreError, match="alias"):
        _routed(tmp_path, resolver, ladybug).plan("q_ladybug")
    assert ladybug.plan_calls == []


def test_missing_binding_refuses_aliased_ladybug_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ladybug_quarantine(tmp_path)
    store, resolver = _resolver(tmp_path)
    ladybug = _RecordingRestore(
        board_id=BOARD_ID,
        board_dir=store.board_ladybug_path(BOARD_ID).parent,
    )
    real_reject = routed_module.reject_filesystem_alias_ancestry

    def reject_target(path: Path) -> None:
        if path == tmp_path / "boards" / BOARD_ID:
            raise ValueError("simulated junction")
        real_reject(path)

    monkeypatch.setattr(
        routed_module,
        "reject_filesystem_alias_ancestry",
        reject_target,
    )

    with pytest.raises(QuarantineRestoreError, match="target crosses"):
        _routed(tmp_path, resolver, ladybug).plan("q_ladybug")
    assert ladybug.plan_calls == []


def test_extensions_preserve_selected_backend(tmp_path: Path) -> None:
    store, resolver = _resolver(tmp_path)
    path = _bind_grafx(store)
    _grafx_wal_quarantine(tmp_path, path)
    ladybug = _RecordingRestore(board_id=BOARD_ID, board_dir=path.parent)
    grafx = _RecordingRestore(board_id=BOARD_ID, board_dir=path)
    adapter = _routed(tmp_path, resolver, ladybug, grafx)

    report = adapter.apply_rebuild_compensation(
        "grafx-wal-route",
        expected_board_id=BOARD_ID,
        run_id="run-1",
        owner_token="owner",
    )
    discarded = adapter.discard_rebuild_candidate(
        expected_board_id=BOARD_ID,
        run_id="run-2",
        owner_token="owner",
    )

    assert report.applied is True
    assert grafx.compensation_calls == [("grafx-wal-route", "run-1")]
    assert grafx.discard_calls == ["run-2"]
    assert discarded["live_absent"] is True


def test_composition_builder_uses_injected_resolver_and_fails_grafx_without_factory(
    tmp_path: Path,
) -> None:
    store, resolver = _resolver(tmp_path)
    path = _bind_grafx(store)
    _grafx_wal_quarantine(tmp_path, path)

    adapter = build_community_routed_quarantine_restore(
        kg_base_dir=str(tmp_path),
        data_dir=str(tmp_path),
        graph_route_resolver=resolver,
        grafx_restore_factory=None,
    )

    with pytest.raises(QuarantineRestoreError, match="dependencies are not composed"):
        adapter.plan("grafx-wal-route")


def test_composition_without_shared_resolver_leaves_optional_slot_fail_closed(
    tmp_path: Path,
) -> None:
    registry = SimpleNamespace(quarantine_restore="poison")

    _apply_quarantine_restore(
        registry,
        kg_base_dir=str(tmp_path / "kg"),
        data_dir=str(tmp_path / "data"),
    )

    assert registry.quarantine_restore is None
    assert list(tmp_path.iterdir()) == []


def test_cli_uses_composed_restore_slot_without_constructing_ladybug(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from okto_pulse.community import cli

    service = _RecordingRestore(board_id=BOARD_ID, board_dir=Path("board"))
    registry = SimpleNamespace(require_quarantine_restore=lambda: service)
    monkeypatch.setattr(
        cli,
        "_configure_kg_restore_cold_registry",
        lambda: registry,
    )
    args = SimpleNamespace(
        quarantine_id="q_cli",
        apply=False,
        json=True,
    )

    with pytest.raises(SystemExit) as captured:
        cli.cmd_kg_restore(args)

    assert captured.value.code == 0
    assert service.plan_calls == ["q_cli"]
    assert json.loads(capsys.readouterr().out)["board_id"] == BOARD_ID


def test_restore_cold_registry_configures_runtime_without_initializing_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.core.services import application_kg

    import okto_pulse.community.adapters.sqlalchemy_database as database
    import okto_pulse.community.config as community_config
    from okto_pulse import core
    from okto_pulse.community import cli
    from okto_pulse.community.adapters import composition

    events: list[str] = []
    factory = object()
    registry = object()

    class Settings:
        database_url = "sqlite+aiosqlite:///restore.db"
        port = 8100

        def __init__(self) -> None:
            events.append("settings")

    def configure_settings(settings) -> None:
        assert isinstance(settings, Settings)
        events.append("configure_settings")

    def configure_relational(settings, *, echo: bool) -> None:
        assert isinstance(settings, Settings)
        assert echo is False
        events.append("configure_relational")

    def get_session_factory():
        events.append("get_session_factory")
        return factory

    def configure_registry(received_factory, *, settings) -> None:
        assert received_factory is factory
        assert isinstance(settings, Settings)
        events.append("configure_registry")

    async def forbidden_init_db() -> None:
        events.append("init_db")

    def get_registry():
        events.append("get_registry")
        return registry

    monkeypatch.setattr(community_config, "CommunitySettings", Settings)
    monkeypatch.setattr(core, "configure_settings", configure_settings)
    monkeypatch.setattr(
        cli,
        "_configure_community_relational_runtime",
        configure_relational,
    )
    monkeypatch.setattr(database, "get_session_factory", get_session_factory)
    monkeypatch.setattr(database, "init_db", forbidden_init_db)
    monkeypatch.setattr(
        composition,
        "configure_community_kg_registry",
        configure_registry,
    )
    monkeypatch.setattr(
        application_kg,
        "get_current_provider_registry",
        get_registry,
    )

    assert cli._configure_kg_restore_cold_registry() is registry
    assert events == [
        "settings",
        "configure_settings",
        "configure_relational",
        "get_session_factory",
        "configure_registry",
        "get_registry",
    ]
