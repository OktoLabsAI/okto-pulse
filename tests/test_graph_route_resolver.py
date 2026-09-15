"""Discriminating contracts for immutable Community graph route resolution."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphCapabilityUnavailable,
    GraphCorruption,
    GraphUnavailable,
)

import okto_pulse.community.adapters.graph_route_resolver as route_module
from okto_pulse.community.adapters.global_discovery_layout import (
    generation_graph_path,
    generations_root,
    switch_active_generation,
    write_generation_manifest,
)
from okto_pulse.community.adapters.graph_backend_binding import (
    CommunityGraphBackendBindingStore,
)
from okto_pulse.community.adapters.graph_route_resolver import (
    CommunityGraphRouteCandidate,
    CommunityGraphRouteResolver,
)


class _FakeGrafxDatabase:
    def __init__(self, path: Path, *, page_size: int) -> None:
        self.path = str(path)
        self.identity = SimpleNamespace(page_size=page_size)


def _ladybug(path: Path, payload: bytes = b"ladybug") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _grafx(path: Path, *, page_size: int = 8192) -> _FakeGrafxDatabase:
    path.mkdir(parents=True, exist_ok=True)
    (path / "grafx.meta").write_bytes(b"grafx")
    return _FakeGrafxDatabase(path, page_size=page_size)


def _make_directory_alias(link: Path, target: Path) -> bool:
    """Create a real directory alias on POSIX or Windows, when permitted."""

    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            return False
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.returncode == 0 and link.exists()
    return link.is_symlink()


def _resolver(
    store: CommunityGraphBackendBindingStore,
    *,
    board_backend: str = "ladybug",
    global_backend: str = "ladybug",
    page_size: int = 8192,
    databases: dict[Path, _FakeGrafxDatabase] | None = None,
    opened: list[Path] | None = None,
) -> CommunityGraphRouteResolver:
    def open_database(path: Path) -> _FakeGrafxDatabase:
        if opened is not None:
            opened.append(path)
        assert databases is not None
        return databases[path]

    return CommunityGraphRouteResolver(
        store,
        board_backend=board_backend,  # type: ignore[arg-type]
        global_backend=global_backend,  # type: ignore[arg-type]
        grafx_page_size=page_size,
        open_grafx_database=open_database if databases is not None else None,
    )


def _publish_active(
    anchor: Path,
    generation: str,
    *,
    backend: str,
    page_size: int | None = None,
) -> Path:
    target = generation_graph_path(anchor, generation)
    if backend == "grafx":
        _grafx(target, page_size=page_size or 8192)
    else:
        _ladybug(target)
    digest, _supported = write_generation_manifest(
        anchor,
        generation,
        {"backend": backend, "page_size": page_size},
    )
    switch_active_generation(
        anchor,
        generation_id=generation,
        manifest_sha256=digest,
    )
    return target


def test_empty_inspection_is_read_only_for_board_and_global(tmp_path: Path) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    resolver = _resolver(store)

    with pytest.raises(GraphCapabilityUnavailable) as board:
        resolver.inspect_board_route("board-1")
    with pytest.raises(GraphCapabilityUnavailable) as global_route:
        resolver.inspect_global_route()

    assert board.value.details["reason"] == "binding_missing"
    assert global_route.value.details["reason"] == "binding_missing"
    assert list(tmp_path.iterdir()) == []


def test_explicit_creation_precedes_binding_publication_and_snapshot_is_frozen(
    tmp_path: Path,
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    resolver = _resolver(store, board_backend="grafx", page_size=4096)
    observed: list[CommunityGraphRouteCandidate] = []

    def create(candidate: CommunityGraphRouteCandidate) -> object:
        observed.append(candidate)
        assert not (
            candidate.binding_path.parent.parent / "graph_backend_binding.json"
        ).exists()
        return _grafx(candidate.binding_path, page_size=4096)

    snapshot = resolver.initialize_board_route("board-create", create_physical=create)

    assert len(observed) == 1
    assert snapshot.backend == "grafx"
    assert snapshot.page_size == 4096
    assert snapshot.binding_path == observed[0].binding_path
    assert len(snapshot.binding_sha256) == len(snapshot.route_sha256) == 64
    assert (
        snapshot.binding_path.parent.parent / "graph_backend_binding.json"
    ).is_file()
    with pytest.raises(FrozenInstanceError):
        snapshot.page_size = 8192  # type: ignore[misc]


def test_persisted_board_binding_and_page_size_override_changed_settings(
    tmp_path: Path,
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    first = _resolver(store, board_backend="grafx", page_size=4096)
    created = first.initialize_board_route(
        "board-pinned",
        create_physical=lambda candidate: _grafx(
            candidate.binding_path, page_size=4096
        ),
    )

    changed = _resolver(store, board_backend="ladybug", page_size=8192)
    inspected = changed.inspect_board_route("board-pinned")

    assert inspected == created
    assert inspected.backend == "grafx"
    assert inspected.page_size == 4096
    assert not store.board_ladybug_path("board-pinned").exists()


def test_physical_before_binding_is_adopted_and_persisted_geometry_wins(
    tmp_path: Path,
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    path = store.board_grafx_path("board-crash", "generation-crash")
    database = _grafx(path, page_size=4096)
    opened: list[Path] = []
    resolver = _resolver(
        store,
        board_backend="ladybug",
        page_size=8192,
        databases={path: database},
        opened=opened,
    )
    creation_calls = 0

    def must_not_create(candidate: CommunityGraphRouteCandidate) -> None:
        nonlocal creation_calls
        creation_calls += 1
        raise AssertionError(candidate)

    snapshot = resolver.initialize_board_route(
        "board-crash", create_physical=must_not_create
    )

    assert snapshot.backend == "grafx"
    assert snapshot.generation == "generation-crash"
    assert snapshot.page_size == 4096
    assert opened == [path]
    assert creation_calls == 0


def test_creation_failure_publishes_no_binding_and_retry_adopts_database(
    tmp_path: Path,
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    path = store.board_grafx_path("board-retry", "generation-1")
    database = _FakeGrafxDatabase(path, page_size=8192)
    first = _resolver(store, board_backend="grafx")

    def create_then_fail(candidate: CommunityGraphRouteCandidate) -> None:
        _grafx(candidate.binding_path)
        raise RuntimeError("injected crash")

    with pytest.raises(GraphUnavailable) as failed:
        first.initialize_board_route("board-retry", create_physical=create_then_fail)
    assert failed.value.details["reason"] == "graph_route_creation_failed"
    binding_path = path.parent.parent / "graph_backend_binding.json"
    assert not binding_path.exists()

    retry = _resolver(store, board_backend="ladybug", databases={path: database})
    adopted = retry.initialize_board_route("board-retry")
    assert adopted.backend == "grafx"
    assert binding_path.is_file()


@pytest.mark.parametrize("scenario", ["both", "multiple_grafx", "illegitimate"])
def test_unbound_ambiguous_storage_fails_without_fallback(
    tmp_path: Path, scenario: str
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    databases: dict[Path, _FakeGrafxDatabase] = {}
    if scenario == "both":
        _ladybug(store.board_ladybug_path("board-ambiguous"))
        path = store.board_grafx_path("board-ambiguous", "generation-1")
        databases[path] = _grafx(path)
    elif scenario == "multiple_grafx":
        for generation in ("generation-1", "generation-2"):
            path = store.board_grafx_path("board-ambiguous", generation)
            databases[path] = _grafx(path)
    else:
        store.board_grafx_path("board-ambiguous", "generation-1").parent.mkdir(
            parents=True
        )
    opened: list[Path] = []
    resolver = _resolver(store, databases=databases, opened=opened)

    with pytest.raises(GraphCapabilityUnavailable) as captured:
        resolver.initialize_board_route(
            "board-ambiguous",
            create_physical=lambda candidate: pytest.fail(str(candidate)),
        )

    assert captured.value.details["reason"] == "graph_route_storage_ambiguous"
    assert opened == []
    assert not (
        store.board_ladybug_path("board-ambiguous").parent
        / "graph_backend_binding.json"
    ).exists()


@pytest.mark.parametrize("suffix", [".wal", ".shadow", ".wal.checkpoint"])
def test_unbound_board_ladybug_sidecar_blocks_grafx_creation_and_publication(
    tmp_path: Path, suffix: str
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    primary = store.board_ladybug_path("board-residue")
    primary.parent.mkdir(parents=True)
    sidecar = primary.with_name(primary.name + suffix)
    sidecar.write_bytes(b"ladybug-residue")
    resolver = _resolver(store, board_backend="grafx")
    creation_calls: list[CommunityGraphRouteCandidate] = []

    with pytest.raises(GraphCapabilityUnavailable) as ambiguous:
        resolver.initialize_board_route(
            "board-residue",
            create_physical=lambda candidate: creation_calls.append(candidate),
        )

    assert ambiguous.value.details["reason"] == "graph_route_storage_ambiguous"
    assert creation_calls == []
    assert sidecar.read_bytes() == b"ladybug-residue"
    assert not (primary.parent / "graph_backend_binding.json").exists()


def test_unbound_board_wal_residue_blocks_existing_grafx_before_open(
    tmp_path: Path,
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    primary = store.board_ladybug_path("board-wal")
    primary.parent.mkdir(parents=True)
    primary.with_name(primary.name + ".wal").write_bytes(b"pending-wal")
    grafx_path = store.board_grafx_path("board-wal", "generation-1")
    database = _grafx(grafx_path)
    opened: list[Path] = []
    resolver = _resolver(
        store,
        board_backend="grafx",
        databases={grafx_path: database},
        opened=opened,
    )

    with pytest.raises(GraphCapabilityUnavailable) as ambiguous:
        resolver.initialize_board_route(
            "board-wal",
            create_physical=lambda candidate: pytest.fail(str(candidate)),
        )

    assert ambiguous.value.details["reason"] == "graph_route_storage_ambiguous"
    assert opened == []
    assert not (primary.parent / "graph_backend_binding.json").exists()


def test_corrupt_binding_never_falls_back_to_other_physical_backend(
    tmp_path: Path,
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    resolver = _resolver(store)
    snapshot = resolver.initialize_board_route(
        "board-corrupt",
        create_physical=lambda candidate: _ladybug(candidate.binding_path),
    )
    alternate = store.board_grafx_path("board-corrupt", "generation-other")
    _grafx(alternate)
    binding_path = snapshot.binding_path.parent / "graph_backend_binding.json"
    document = json.loads(binding_path.read_text(encoding="utf-8"))
    document["backend"] = "grafx"
    binding_path.write_text(json.dumps(document), encoding="utf-8")
    opened: list[Path] = []
    changed = _resolver(
        store,
        board_backend="grafx",
        databases={alternate: _FakeGrafxDatabase(alternate, page_size=8192)},
        opened=opened,
    )

    with pytest.raises(GraphCorruption):
        changed.inspect_board_route("board-corrupt")
    with pytest.raises(GraphCorruption):
        changed.initialize_board_route("board-corrupt")

    assert opened == []


def test_board_and_global_initialization_have_independent_route_locks(
    tmp_path: Path,
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    resolver = _resolver(store)
    initialized_global = []

    def create_board(candidate: CommunityGraphRouteCandidate) -> Path:
        initialized_global.append(
            resolver.initialize_global_route(
                create_physical=lambda global_candidate: _ladybug(
                    global_candidate.binding_path
                )
            )
        )
        return _ladybug(candidate.binding_path)

    board = resolver.initialize_board_route(
        "board-independent", create_physical=create_board
    )

    assert board.scope == "board"
    assert initialized_global[0].scope == "global"
    assert board.binding_sha256 != initialized_global[0].binding_sha256


def test_global_grafx_snapshot_separates_anchor_and_authenticated_active_target(
    tmp_path: Path,
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    anchor = store.global_grafx_path("generation-1")
    anchor_database = _grafx(anchor, page_size=4096)
    resolver = _resolver(
        store,
        global_backend="grafx",
        page_size=4096,
        databases={anchor: anchor_database},
    )
    resolver.initialize_global_route(create_physical=lambda _candidate: anchor_database)
    active_path = _publish_active(
        anchor, "gdr_active1", backend="grafx", page_size=4096
    )
    active_database = _FakeGrafxDatabase(active_path, page_size=4096)

    snapshot = resolver.inspect_global_route()
    admission = resolver.admit_grafx_route(
        snapshot, active_database, operation="test_global_route"
    )

    assert snapshot.binding_path == snapshot.anchor_path == anchor
    assert snapshot.active_path == active_path
    assert snapshot.active_generation == "gdr_active1"
    assert snapshot.active_manifest_sha256 is not None
    assert admission.page_size == 4096

    with pytest.raises(GraphCapabilityUnavailable) as wrong_path:
        resolver.admit_grafx_route(
            snapshot,
            _FakeGrafxDatabase(anchor, page_size=4096),
            operation="test_global_route",
        )
    assert wrong_path.value.details["reason"] == "grafx_database_path_mismatch"

    with pytest.raises(GraphCapabilityUnavailable) as wrong_page:
        resolver.admit_grafx_route(
            snapshot,
            _FakeGrafxDatabase(active_path, page_size=8192),
            operation="test_global_route",
        )
    assert (
        wrong_page.value.details["reason"] == "grafx_page_size_configuration_mismatch"
    )


def test_unbound_global_adopts_authenticated_active_grafx_and_its_geometry(
    tmp_path: Path,
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    anchor = store.global_grafx_path("generation-crash")
    anchor_database = _grafx(anchor, page_size=4096)
    active_path = _publish_active(anchor, "gdr_crash", backend="grafx", page_size=4096)
    active_database = _FakeGrafxDatabase(active_path, page_size=4096)
    opened: list[Path] = []
    resolver = _resolver(
        store,
        global_backend="ladybug",
        page_size=8192,
        databases={anchor: anchor_database, active_path: active_database},
        opened=opened,
    )

    snapshot = resolver.initialize_global_route()

    assert snapshot.backend == "grafx"
    assert snapshot.generation == "generation-crash"
    assert snapshot.page_size == 4096
    assert snapshot.binding_path == snapshot.anchor_path == anchor
    assert snapshot.active_path == active_path
    assert opened == [anchor, active_path]


def test_unbound_global_rejects_active_grafx_page_mismatch_without_binding(
    tmp_path: Path,
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    anchor = store.global_grafx_path("generation-crash")
    anchor_database = _grafx(anchor, page_size=4096)
    active_path = _publish_active(
        anchor, "gdr_mismatch", backend="grafx", page_size=8192
    )
    active_database = _FakeGrafxDatabase(active_path, page_size=8192)
    resolver = _resolver(
        store,
        global_backend="grafx",
        databases={anchor: anchor_database, active_path: active_database},
    )

    with pytest.raises(GraphCapabilityUnavailable) as mismatch:
        resolver.initialize_global_route()

    assert mismatch.value.details["reason"] == "graph_route_storage_ambiguous"
    assert not (anchor.parent.parent / "graph_backend_binding.json").exists()


@pytest.mark.parametrize("scenario", ["both_backends", "multiple_grafx_anchors"])
def test_unbound_global_ambiguous_physical_routes_never_open_or_publish(
    tmp_path: Path, scenario: str
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    databases: dict[Path, _FakeGrafxDatabase] = {}
    first = store.global_grafx_path("generation-1")
    databases[first] = _grafx(first)
    if scenario == "both_backends":
        _ladybug(store.global_ladybug_path())
    else:
        second = store.global_grafx_path("generation-2")
        databases[second] = _grafx(second)
    opened: list[Path] = []
    resolver = _resolver(store, databases=databases, opened=opened)

    with pytest.raises(GraphCapabilityUnavailable) as ambiguous:
        resolver.initialize_global_route()

    assert ambiguous.value.details["reason"] == "graph_route_storage_ambiguous"
    assert opened == []
    assert not (
        store.global_ladybug_path().parent / "graph_backend_binding.json"
    ).exists()


def test_unbound_global_wal_residue_blocks_grafx_creation_and_publication(
    tmp_path: Path,
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    primary = store.global_ladybug_path()
    primary.parent.mkdir(parents=True)
    wal = primary.with_name(primary.name + ".wal")
    wal.write_bytes(b"pending-global-wal")
    resolver = _resolver(store, global_backend="grafx")
    creation_calls: list[CommunityGraphRouteCandidate] = []

    with pytest.raises(GraphCapabilityUnavailable) as ambiguous:
        resolver.initialize_global_route(
            create_physical=lambda candidate: creation_calls.append(candidate)
        )

    assert ambiguous.value.details["reason"] == "graph_route_storage_ambiguous"
    assert creation_calls == []
    assert wal.read_bytes() == b"pending-global-wal"
    assert not (primary.parent / "graph_backend_binding.json").exists()


def test_unbound_global_wal_residue_blocks_existing_grafx_before_open(
    tmp_path: Path,
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    primary = store.global_ladybug_path()
    primary.parent.mkdir(parents=True)
    primary.with_name(primary.name + ".wal").write_bytes(b"pending-global-wal")
    grafx_path = store.global_grafx_path("generation-1")
    database = _grafx(grafx_path)
    opened: list[Path] = []
    resolver = _resolver(
        store,
        global_backend="grafx",
        databases={grafx_path: database},
        opened=opened,
    )

    with pytest.raises(GraphCapabilityUnavailable) as ambiguous:
        resolver.initialize_global_route()

    assert ambiguous.value.details["reason"] == "graph_route_storage_ambiguous"
    assert opened == []
    assert not (primary.parent / "graph_backend_binding.json").exists()


@pytest.mark.parametrize(
    ("scope", "alias_level"),
    [("board", "root"), ("board", "scope_parent"), ("global", "scope_parent")],
)
def test_route_initialization_alias_gate_precedes_every_filesystem_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    scope: str,
    alias_level: str,
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    if scope == "board":
        scope_parent = store.board_ladybug_path("board-alias").parent
    else:
        scope_parent = store.global_ladybug_path().parent
    alias_target = tmp_path if alias_level == "root" else scope_parent
    resolver = _resolver(store, board_backend="grafx", global_backend="grafx")
    alias_probes: list[Path] = []
    mkdir_calls: list[Path] = []
    file_lock_calls: list[tuple[object, ...]] = []
    creation_calls: list[CommunityGraphRouteCandidate] = []

    def alias_probe(path: Path) -> bool:
        alias_probes.append(path)
        return path == alias_target

    def forbidden_mkdir(path: Path, *args: object, **kwargs: object) -> None:
        mkdir_calls.append(path)
        raise AssertionError((args, kwargs))

    def forbidden_file_lock(*args: object, **kwargs: object) -> None:
        file_lock_calls.append(args)
        raise AssertionError(kwargs)

    monkeypatch.setattr(route_module, "is_filesystem_alias", alias_probe)
    monkeypatch.setattr(Path, "mkdir", forbidden_mkdir)
    monkeypatch.setattr(route_module, "FileLock", forbidden_file_lock)

    with pytest.raises(GraphCorruption) as refused:
        if scope == "board":
            resolver.initialize_board_route(
                "board-alias",
                create_physical=lambda candidate: creation_calls.append(candidate),
            )
        else:
            resolver.initialize_global_route(
                create_physical=lambda candidate: creation_calls.append(candidate)
            )

    assert refused.value.details["reason"] == "graph_route_filesystem_alias_refused"
    assert alias_target in alias_probes
    assert mkdir_calls == []
    assert file_lock_calls == []
    assert creation_calls == []
    assert list(tmp_path.iterdir()) == []


def test_global_missing_anchor_remains_inspectable_but_not_acquirable(
    tmp_path: Path,
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    anchor = store.global_grafx_path("generation-1")
    anchor_database = _grafx(anchor)
    resolver = _resolver(
        store, global_backend="grafx", databases={anchor: anchor_database}
    )
    resolver.initialize_global_route(create_physical=lambda _candidate: anchor_database)
    active_path = _publish_active(anchor, "gdr_recover", backend="grafx")
    active_database = _FakeGrafxDatabase(active_path, page_size=8192)
    (anchor / "grafx.meta").unlink()
    anchor.rmdir()

    inspected = resolver.inspect_global_route()

    assert inspected.binding_path == inspected.anchor_path == anchor
    assert inspected.active_path == active_path
    assert (
        resolver.admit_grafx_route(
            inspected, active_database, operation="recover_global_route"
        ).page_size
        == 8192
    )
    with pytest.raises(GraphUnavailable) as unavailable:
        resolver.acquire_global_route()
    assert unavailable.value.details["reason"] == "physical_database_missing"


def test_pinned_grafx_board_revalidation_reuses_the_acquired_physical_proof(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    resolver = _resolver(store, board_backend="grafx")
    database: _FakeGrafxDatabase | None = None

    def create(candidate: CommunityGraphRouteCandidate) -> _FakeGrafxDatabase:
        nonlocal database
        database = _grafx(candidate.binding_path)
        return database

    snapshot = resolver.initialize_board_route(
        "board-pinned-revalidation",
        create_physical=create,
    )
    assert database is not None

    def duplicate_route_walk(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the acquired binding already authenticated this path")

    monkeypatch.setattr(resolver, "_require_expected_path", duplicate_route_walk)

    assert resolver.revalidate_pinned_grafx_board_snapshot(snapshot, database) == snapshot


def test_pinned_grafx_board_revalidation_detects_binding_cutover(
    tmp_path: Path,
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    resolver = _resolver(store, board_backend="grafx")
    original_database: _FakeGrafxDatabase | None = None

    def create(candidate: CommunityGraphRouteCandidate) -> _FakeGrafxDatabase:
        nonlocal original_database
        original_database = _grafx(candidate.binding_path)
        return original_database

    original = resolver.initialize_board_route(
        "board-pinned-cutover",
        create_physical=create,
    )
    assert original_database is not None
    replacement_path = store.board_grafx_path(
        "board-pinned-cutover",
        "generation-2",
    )
    replacement_database = _grafx(replacement_path)
    store.compare_and_swap_board_binding(
        board_id="board-pinned-cutover",
        expected_binding_sha256=original.binding_sha256,
        backend="grafx",
        generation="generation-2",
        physical_path=replacement_path,
        page_size=8192,
        database=replacement_database,
    )

    with pytest.raises(GraphCapabilityUnavailable) as mismatch:
        resolver.revalidate_pinned_grafx_board_snapshot(
            original,
            original_database,
        )

    assert mismatch.value.details["reason"] == "graph_route_snapshot_mismatch"


def test_pinned_grafx_board_revalidation_still_requires_the_physical_database(
    tmp_path: Path,
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    resolver = _resolver(store, board_backend="grafx")
    database: _FakeGrafxDatabase | None = None

    def create(candidate: CommunityGraphRouteCandidate) -> _FakeGrafxDatabase:
        nonlocal database
        database = _grafx(candidate.binding_path)
        return database

    snapshot = resolver.initialize_board_route(
        "board-pinned-missing",
        create_physical=create,
    )
    assert database is not None
    (snapshot.active_path / "grafx.meta").unlink()
    snapshot.active_path.rmdir()

    with pytest.raises(GraphUnavailable) as unavailable:
        resolver.revalidate_pinned_grafx_board_snapshot(snapshot, database)

    assert unavailable.value.details["reason"] == "physical_database_missing"


def test_pinned_grafx_board_revalidation_refuses_a_physical_path_alias(
    tmp_path: Path,
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    resolver = _resolver(store, board_backend="grafx")
    database: _FakeGrafxDatabase | None = None

    def create(candidate: CommunityGraphRouteCandidate) -> _FakeGrafxDatabase:
        nonlocal database
        database = _grafx(candidate.binding_path)
        return database

    snapshot = resolver.initialize_board_route(
        "board-pinned-alias",
        create_physical=create,
    )
    assert database is not None
    alias_target = tmp_path / "aliased-generation"
    snapshot.active_path.rename(alias_target)
    if not _make_directory_alias(snapshot.active_path, alias_target):
        pytest.skip("this environment cannot create a directory alias")

    with pytest.raises(GraphCorruption) as refused:
        resolver.revalidate_pinned_grafx_board_snapshot(snapshot, database)

    assert refused.value.details["reason"] == "binding_document_invalid"


@pytest.mark.parametrize(
    ("database", "reason"),
    [
        (_FakeGrafxDatabase(Path("D:/foreign/grafx"), page_size=8192), "grafx_database_path_mismatch"),
        (_FakeGrafxDatabase(Path("D:/unused"), page_size=4096), "grafx_page_size_configuration_mismatch"),
    ],
)
def test_pinned_grafx_board_revalidation_readmits_path_and_page_size(
    tmp_path: Path,
    database: _FakeGrafxDatabase,
    reason: str,
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    resolver = _resolver(store, board_backend="grafx")
    snapshot = resolver.initialize_board_route(
        "board-pinned-admission",
        create_physical=lambda candidate: _grafx(candidate.binding_path),
    )
    if reason == "grafx_page_size_configuration_mismatch":
        database.path = str(snapshot.active_path)

    with pytest.raises(GraphCapabilityUnavailable) as refused:
        resolver.revalidate_pinned_grafx_board_snapshot(snapshot, database)

    assert refused.value.details["reason"] == reason


def test_pinned_grafx_board_revalidation_compares_the_complete_snapshot(
    tmp_path: Path,
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    resolver = _resolver(store, board_backend="grafx")
    database: _FakeGrafxDatabase | None = None

    def create(candidate: CommunityGraphRouteCandidate) -> _FakeGrafxDatabase:
        nonlocal database
        database = _grafx(candidate.binding_path)
        return database

    snapshot = resolver.initialize_board_route(
        "board-pinned-complete-snapshot",
        create_physical=create,
    )
    assert database is not None
    forged = replace(snapshot, route_sha256="f" * 64)

    with pytest.raises(GraphCapabilityUnavailable) as mismatch:
        resolver.revalidate_pinned_grafx_board_snapshot(forged, database)

    assert mismatch.value.details["reason"] == "graph_route_snapshot_mismatch"


def test_pinned_grafx_board_revalidation_propagates_binding_corruption(
    tmp_path: Path,
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    resolver = _resolver(store, board_backend="grafx")
    database: _FakeGrafxDatabase | None = None

    def create(candidate: CommunityGraphRouteCandidate) -> _FakeGrafxDatabase:
        nonlocal database
        database = _grafx(candidate.binding_path)
        return database

    snapshot = resolver.initialize_board_route(
        "board-pinned-corrupt-binding",
        create_physical=create,
    )
    assert database is not None
    binding_path = snapshot.active_path.parents[1] / "graph_backend_binding.json"
    binding_path.write_text('{"broken":true}', encoding="utf-8")

    with pytest.raises(GraphCorruption) as corrupt:
        resolver.revalidate_pinned_grafx_board_snapshot(snapshot, database)

    assert corrupt.value.details["reason"] == "binding_document_invalid"


@pytest.mark.parametrize(
    "invalid",
    [
        {"scope": "global", "scope_id": "global"},
        {"backend": "ladybug", "page_size": None},
        {"page_size": None},
    ],
)
def test_pinned_grafx_board_revalidation_rejects_the_wrong_route_kind_first(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    invalid: dict[str, object],
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    resolver = _resolver(store, board_backend="grafx")
    database: _FakeGrafxDatabase | None = None

    def create(candidate: CommunityGraphRouteCandidate) -> _FakeGrafxDatabase:
        nonlocal database
        database = _grafx(candidate.binding_path)
        return database

    snapshot = resolver.initialize_board_route(
        "board-pinned-route-kind",
        create_physical=create,
    )
    assert database is not None
    invalid_snapshot = replace(snapshot, **invalid)  # type: ignore[arg-type]
    monkeypatch.setattr(
        store,
        "acquire_board_binding",
        lambda _board_id: pytest.fail("invalid route touched the binding store"),
    )

    with pytest.raises(GraphCapabilityUnavailable) as refused:
        resolver.revalidate_pinned_grafx_board_snapshot(invalid_snapshot, database)

    assert refused.value.details["reason"] == "pinned_grafx_board_route_required"


def test_generic_board_revalidation_keeps_the_full_resolver_walk(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    resolver = _resolver(store, board_backend="grafx")
    snapshot = resolver.initialize_board_route(
        "board-generic-revalidation",
        create_physical=lambda candidate: _grafx(candidate.binding_path),
    )
    original = resolver._require_expected_path
    walked: list[Path] = []

    def counted(path: Path, **kwargs: object) -> None:
        walked.append(path)
        original(path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(resolver, "_require_expected_path", counted)

    assert resolver.revalidate_snapshot(snapshot, require_physical=True) == snapshot
    assert walked == [snapshot.active_path]


def test_global_pointer_cutover_invalidates_snapshot_without_binding_fallback(
    tmp_path: Path,
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    anchor = store.global_grafx_path("generation-1")
    database = _grafx(anchor)
    resolver = _resolver(store, global_backend="grafx", databases={anchor: database})
    original = resolver.initialize_global_route(
        create_physical=lambda _candidate: database
    )
    _publish_active(anchor, "gdr_cutover", backend="grafx")

    current = resolver.inspect_global_route()
    with pytest.raises(GraphCapabilityUnavailable) as mismatch:
        resolver.revalidate_snapshot(original)

    assert original.binding_sha256 == current.binding_sha256
    assert original.route_sha256 != current.route_sha256
    assert mismatch.value.details["reason"] == "graph_route_snapshot_mismatch"


def test_global_ladybug_binding_stays_on_anchor_across_pointer_cutovers(
    tmp_path: Path,
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    anchor = store.global_ladybug_path()
    _ladybug(anchor)
    first_path = _publish_active(anchor, "gdr_lady1", backend="ladybug")
    resolver = _resolver(store)
    adopted = resolver.initialize_global_route()
    assert adopted.binding_path == adopted.anchor_path == anchor
    assert adopted.generation == "generation-1"
    assert adopted.active_generation == "gdr_lady1"
    assert adopted.active_path == first_path

    second_path = _publish_active(anchor, "gdr_lady2", backend="ladybug")
    current = resolver.inspect_global_route()

    assert current.binding_path == current.anchor_path == anchor
    assert current.generation == "generation-1"
    assert current.binding_sha256 == adopted.binding_sha256
    assert current.active_generation == "gdr_lady2"
    assert current.active_path == second_path
    assert current.route_sha256 != adopted.route_sha256
    with pytest.raises(GraphCapabilityUnavailable) as stale:
        resolver.revalidate_snapshot(adopted)
    assert stale.value.details["reason"] == "graph_route_snapshot_mismatch"


def test_global_generations_root_alias_is_refused_before_layout_reader(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    anchor = _ladybug(store.global_ladybug_path())
    resolver = _resolver(store)
    resolver.initialize_global_route()
    _publish_active(anchor, "gdr_alias", backend="ladybug")
    generation_root = generations_root(anchor)
    real_is_alias = route_module.is_filesystem_alias
    reader_calls: list[Path] = []

    def alias_probe(path: Path) -> bool:
        return path == generation_root or real_is_alias(path)

    def forbidden_reader(path: Path) -> None:
        reader_calls.append(path)
        raise AssertionError("layout reader crossed the aliased generations root")

    monkeypatch.setattr(route_module, "is_filesystem_alias", alias_probe)
    monkeypatch.setattr(route_module, "read_active_generation", forbidden_reader)

    with pytest.raises(GraphCorruption) as refused:
        resolver.inspect_global_route()

    assert refused.value.details["reason"] == "graph_route_filesystem_alias_refused"
    assert reader_calls == []


def test_global_malformed_pointer_fails_closed_even_when_anchor_is_missing(
    tmp_path: Path,
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    anchor = store.global_grafx_path("generation-1")
    database = _grafx(anchor)
    resolver = _resolver(store, global_backend="grafx", databases={anchor: database})
    resolver.initialize_global_route(create_physical=lambda _candidate: database)
    pointer = anchor.parent / "active_generation.json"
    pointer.write_text('{"pointer_sha256":"bad"}', encoding="utf-8")
    generations_root(anchor).mkdir()
    (anchor / "grafx.meta").unlink()
    anchor.rmdir()

    with pytest.raises(GraphCorruption) as captured:
        resolver.inspect_global_route()

    assert (
        captured.value.details["reason"] == "global_route_pointer_or_manifest_invalid"
    )
