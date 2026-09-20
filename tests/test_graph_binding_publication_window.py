"""Real process mutex/publication APIs; metadata stubs never open a graph."""

import subprocess
import sys
from types import SimpleNamespace

import pytest

from okto_pulse.core.kg.interfaces.graph_errors import GraphLockContention, GraphUnavailable
from okto_pulse.community.adapters.graph_backend_binding import (
    BINDING_PUBLICATION_MUTEX_FILENAME, CommunityGraphBackendBindingStore,
    GraphBindingCompareAndSwapConflict,
)
from okto_pulse.community.adapters.grafx_board_storage import (
    grafx_board_privacy_scope, grafx_board_privacy_storage_present,
)


def candidate(store, scope, generation):
    path = store.board_grafx_path("board", generation) if scope == "board" else store.global_grafx_path(generation)
    path.mkdir(parents=True)
    (path / "grafx.meta").write_bytes(b"metadata fixture, never opened")
    return dict(backend="grafx", generation=generation, physical_path=path, page_size=8192,
        database=SimpleNamespace(path=str(path), identity=SimpleNamespace(page_size=8192)))


def initialize(store, scope, options):
    return (store.initialize_board_binding(board_id="board", **options) if scope == "board"
            else store.initialize_global_binding(**options))


def child(root, scope="board", mode="window", expected=""):
    script = """
from pathlib import Path
from types import SimpleNamespace
import sys
from okto_pulse.core.kg.interfaces.graph_errors import GraphLockContention
from okto_pulse.community.adapters.graph_backend_binding import CommunityGraphBackendBindingStore
store = CommunityGraphBackendBindingStore(sys.argv[1], lock_timeout_seconds=0.05)
scope, mode, expected = sys.argv[2:]
try:
    if mode == 'window':
        with store.publication_window():
            print('entered')
    else:
        path = store.board_grafx_path('board','candidate') if scope == 'board' else store.global_grafx_path('candidate')
        options = dict(backend='grafx',generation='candidate',physical_path=path,page_size=8192,
            database=SimpleNamespace(path=str(path),identity=SimpleNamespace(page_size=8192)))
        if scope == 'board':
            options['board_id'] = 'board'
        if mode == 'cas':
            options['expected_binding_sha256'] = expected
            method = store.compare_and_swap_board_binding if scope == 'board' else store.compare_and_swap_global_binding
        else:
            method = store.initialize_board_binding if scope == 'board' else store.initialize_global_binding
        method(**options)
        print('published')
except GraphLockContention:
    print('blocked')
"""
    return subprocess.run([sys.executable, "-c", script, str(root), scope, mode, expected],
        text=True, capture_output=True, check=True, timeout=20).stdout.strip()


def test_empty_window_preserves_absent_routes_and_privacy_absence(tmp_path):
    store = CommunityGraphBackendBindingStore(tmp_path)
    scope = grafx_board_privacy_scope("absent", tmp_path / "boards" / "absent")
    assert not grafx_board_privacy_storage_present(scope)
    with store.publication_window() as owner:
        assert owner is store
        assert child(tmp_path) == "blocked"
        assert not (tmp_path / "boards").exists()
        assert not (tmp_path / "global").exists()
        assert not grafx_board_privacy_storage_present(scope)
    assert child(tmp_path) == "entered"
    # FileLock may remove its empty sidecar on Windows; neither form is data.
    assert {path.name for path in tmp_path.iterdir()} <= {BINDING_PUBLICATION_MUTEX_FILENAME}


@pytest.mark.parametrize("scope", ["board", "global"])
@pytest.mark.parametrize("mode", ["initialize", "cas"])
def test_other_process_publication_is_excluded_then_released(tmp_path, scope, mode):
    store = CommunityGraphBackendBindingStore(tmp_path)
    expected = initialize(store, scope, candidate(store, scope, "old")).binding_sha256 if mode == "cas" else ""
    candidate(store, scope, "candidate")
    folder = tmp_path / "boards" / "board" if scope == "board" else tmp_path / "global"
    binding = folder / "graph_backend_binding.json"
    before = binding.read_bytes() if binding.exists() else None
    with store.publication_window():
        assert child(tmp_path, scope, mode, expected) == "blocked"
        assert (binding.read_bytes() if binding.exists() else None) == before
    assert child(tmp_path, scope, mode, expected) == "published"


def test_owner_cas_is_reentrant_but_keeps_expected_digest_guard(tmp_path):
    store = CommunityGraphBackendBindingStore(tmp_path, lock_timeout_seconds=0.05)
    original = initialize(store, "board", candidate(store, "board", "old"))
    options = candidate(store, "board", "candidate")
    with store.publication_window():
        updated = store.compare_and_swap_board_binding(board_id="board", expected_binding_sha256=original.binding_sha256, **options)
        with pytest.raises(GraphBindingCompareAndSwapConflict):
            store.compare_and_swap_board_binding(board_id="board", expected_binding_sha256=original.binding_sha256, **options)
        with pytest.raises(GraphLockContention):
            with CommunityGraphBackendBindingStore(tmp_path, lock_timeout_seconds=0.05).publication_window():
                pytest.fail("another store is not the window owner")
        assert store.inspect_board_binding("board") == updated


def test_body_failure_releases_and_other_roots_are_independent(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(RuntimeError, match="injected"):
        with CommunityGraphBackendBindingStore(tmp_path).publication_window():
            assert child(other) == "entered"
            raise RuntimeError("injected")
    assert child(tmp_path) == "entered"


def test_missing_root_is_not_created(tmp_path):
    missing = tmp_path / "missing"
    with pytest.raises(GraphUnavailable):
        with CommunityGraphBackendBindingStore(missing).publication_window():
            pytest.fail("missing root")
    assert not missing.exists()


def test_mutex_alias_refused_without_touching_target(tmp_path):
    unrelated = tmp_path / "unrelated"
    unrelated.write_bytes(b"keep exact bytes")
    try:
        (tmp_path / BINDING_PUBLICATION_MUTEX_FILENAME).symlink_to(unrelated)
    except OSError as error:
        pytest.skip(str(error))
    with pytest.raises(ValueError):
        with CommunityGraphBackendBindingStore(tmp_path).publication_window():
            pytest.fail("alias")
    assert unrelated.read_bytes() == b"keep exact bytes"


@pytest.mark.parametrize("content", [b"x", b"opaque pre-existing data", None])
def test_occupied_mutex_path_is_not_claimed_or_erased(tmp_path, content):
    path = tmp_path / BINDING_PUBLICATION_MUTEX_FILENAME
    if content is None:
        path.mkdir()
    else:
        path.write_bytes(content)
    with pytest.raises(ValueError, match="mutex_path_occupied"):
        with CommunityGraphBackendBindingStore(tmp_path).publication_window():
            pytest.fail("pre-existing data is not a mutex")
    assert path.is_dir() if content is None else path.read_bytes() == content
