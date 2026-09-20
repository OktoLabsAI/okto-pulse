"""Routing census tests; placeholder files are never opened as native graphs."""

from contextlib import closing
import json
import sqlite3
from types import SimpleNamespace

import pytest
from okto_pulse.core.kg.interfaces.graph_errors import GraphCorruption

from okto_pulse.community.adapters.graph_backend_binding import CommunityGraphBackendBindingStore
from okto_pulse.community.adapters.recovery_graph_inventory import (
    read_recovery_graph_inventory, recovery_graph_inventory_from_manifest,
    require_recovery_graph_selection,
)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "kg"
    root.mkdir()
    with closing(sqlite3.connect(tmp_path / "data.sqlite3")) as connection:
        connection.execute("CREATE TABLE boards(id TEXT PRIMARY KEY)")
        connection.executemany("INSERT INTO boards VALUES (?)", [("a",), ("empty",)])
        connection.commit()
        connection.execute("BEGIN")
        yield connection, root, CommunityGraphBackendBindingStore(root)


def bind(store, board_id="a"):
    path = store.board_grafx_path(board_id, "g1") if board_id else store.global_grafx_path("g1")
    path.mkdir(parents=True)
    (path / "grafx.meta").write_bytes(b"placeholder identity for filesystem census only")
    database = SimpleNamespace(path=str(path), identity=SimpleNamespace(page_size=8192))
    options = dict(backend="grafx", generation="g1", physical_path=path, page_size=8192, database=database)
    return (store.initialize_board_binding(board_id=board_id, **options) if board_id
            else store.initialize_global_binding(**options))


def files(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_census_preserves_files_and_exposes_absence_and_retention_locations(source):
    connection, root, store = source
    board, global_graph = bind(store), bind(store, None)
    old = store.board_grafx_path("a", "previous")
    old.mkdir()
    (old / "grafx.meta").write_bytes(b"older generation")
    (root / "quarantine").mkdir()
    (root / "boards" / "a" / "historical.bin").write_bytes(b"keep")
    before, sql_before = files(root), tuple(connection.iterdump())
    census = read_recovery_graph_inventory(connection, root)
    assert census.board_ids == ("a", "empty")
    assert [route.state for route in census.routes] == ["bound", "binding_absent_storage_absent", "bound"]
    assert census.unselected_generation_paths == ("boards/a/grafx/previous",)
    assert census.other_storage_paths == ("boards/a/historical.bin", "quarantine")
    assert census.issues == ()
    selected = (("board", "a", str(board.physical_path), 8192),
                ("global_discovery", None, str(global_graph.physical_path), 8192))
    require_recovery_graph_selection(census, selected)
    restored = recovery_graph_inventory_from_manifest(json.loads(json.dumps(census.as_manifest())))
    assert restored == census
    for wrong in (selected[:1], selected + selected[:1], (selected[0][:-1] + (16384,), selected[1])):
        with pytest.raises(ValueError, match="selection_mismatch"):
            require_recovery_graph_selection(census, wrong)
    assert files(root) == before
    assert tuple(connection.iterdump()) == sql_before
    assert not (root / "boards" / "empty").exists()


@pytest.mark.parametrize("case", ["orphan", "unbound", "missing_generation", "missing_identity"])
def test_unresolved_storage_cannot_be_accepted_as_complete_selection(source, case):
    connection, root, store = source
    if case == "orphan":
        (root / "boards" / "unknown").mkdir(parents=True)
    elif case == "unbound":
        store.board_grafx_path("a", "g1").mkdir(parents=True)
    else:
        binding = bind(store)
        (binding.physical_path / "grafx.meta").unlink()
        if case == "missing_generation":
            binding.physical_path.rmdir()
    before = files(root)
    census = read_recovery_graph_inventory(connection, root)
    assert census.issues
    with pytest.raises(ValueError, match="inventory_unresolved"):
        require_recovery_graph_selection(census, ())
    assert files(root) == before


def test_corrupt_binding_fails_without_repair(source):
    connection, root, store = source
    bind(store)
    binding = root / "boards" / "a" / "graph_backend_binding.json"
    value = json.loads(binding.read_bytes())
    value["generation"] = "forged"
    binding.write_text(json.dumps(value), encoding="utf-8")
    before = files(root)
    with pytest.raises(GraphCorruption):
        read_recovery_graph_inventory(connection, root)
    assert files(root) == before


def test_shared_budget_counts_boards_and_directory_entries(source):
    connection, root, store = source
    bind(store)
    with pytest.raises(ValueError, match="entry_limit"):
        read_recovery_graph_inventory(connection, root, max_entries=2)


def test_transaction_and_identity_schema_required(source):
    connection, root, _ = source
    connection.rollback()
    with pytest.raises(ValueError, match="transaction_required"):
        read_recovery_graph_inventory(connection, root)
    connection.execute("DROP TABLE boards")
    connection.execute("CREATE TABLE boards(id TEXT, secondary TEXT, PRIMARY KEY(id, secondary))")
    connection.execute("BEGIN")
    with pytest.raises(ValueError, match="schema_drift"):
        read_recovery_graph_inventory(connection, root)


@pytest.mark.parametrize("case", ["root", "board", "identity"])
def test_aliases_are_not_followed(source, tmp_path, case):
    connection, root, store = source
    target = tmp_path / "target"
    target.mkdir()
    marker = target / "marker"
    marker.write_bytes(b"unrelated")
    if case == "root":
        link = tmp_path / "root-alias"
    elif case == "board":
        (root / "boards").mkdir()
        link = root / "boards" / "a"
    else:
        binding = bind(store)
        link = binding.physical_path / "grafx.meta"
        link.unlink()
    try:
        link.symlink_to(marker if case == "identity" else target, target_is_directory=case != "identity")
    except OSError as failure:
        pytest.skip(f"symlink creation unavailable: {failure}")
    with pytest.raises(ValueError):
        read_recovery_graph_inventory(connection, link if case == "root" else root)
    assert marker.read_bytes() == b"unrelated"


def test_manifest_cannot_omit_a_board_route(source):
    connection, root, _ = source
    value = read_recovery_graph_inventory(connection, root).as_manifest()
    value["routes"].pop(0)
    with pytest.raises(ValueError, match="scope_mismatch"):
        recovery_graph_inventory_from_manifest(value)
