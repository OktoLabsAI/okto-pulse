"""Read-only reconciliation of relational Boards and persisted graph routing.

No route initialization, inferred ownership or generation promotion occurs.
This inventories active routing plus locations still requiring preservation;
listing an inactive generation or another file does not back up its contents.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import sqlite3

from okto_pulse.community.adapters.graph_backend_binding import (
    BINDING_PUBLICATION_MUTEX_FILENAME, BOARD_BINDING_FILENAME, CommunityGraphBackendBindingStore,
)
from okto_pulse.community.adapters.relational_recovery_snapshot import _digest, _path


@dataclass(frozen=True, slots=True)
class RecoveryGraphRoute:
    scope: str
    board_id: str | None
    state: str
    generation: str | None = None
    physical_path: str | None = None
    page_size: int | None = None
    binding_sha256: str | None = None
    identity_file_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class RecoveryGraphInventory:
    kg_base_dir: str
    board_ids: tuple[str, ...]
    routes: tuple[RecoveryGraphRoute, ...]
    unselected_generation_paths: tuple[str, ...]
    other_storage_paths: tuple[str, ...]
    issues: tuple[str, ...]

    def require_resolved_routes(self) -> None:
        if self.issues:
            raise ValueError("recovery_graph_inventory_unresolved: " + "; ".join(self.issues))

    def as_manifest(self) -> dict:
        # Convert tuple containers to the same JSON representation used by the
        # authenticated manifest. No path is used as an instruction on restore.
        value = asdict(self)
        value["board_ids"] = list(self.board_ids)
        value["routes"] = [asdict(route) for route in self.routes]
        value["unselected_generation_paths"] = list(self.unselected_generation_paths)
        value["other_storage_paths"] = list(self.other_storage_paths)
        value["issues"] = list(self.issues)
        return value


def read_recovery_graph_inventory(
    connection: sqlite3.Connection, kg_base_dir: Path, *, max_entries: int = 100_000,
) -> RecoveryGraphInventory:
    """Read under the caller's pinned relational transaction, with shared bounds.

    The capture coordinator must re-read this inventory under its reservation
    before publication. This function alone cannot fence filesystem changes.
    Missing bindings with no observed generation/storage remain explicit absence,
    not an instruction to initialize a graph or proof of migration readiness.
    """
    if not connection.in_transaction:
        raise ValueError("recovery_graph_inventory_transaction_required")
    if type(max_entries) is not int or not 1 <= max_entries <= 1_000_000:
        raise ValueError("recovery_graph_inventory_limit_invalid")
    supplied = Path(kg_base_dir)
    if not supplied.is_absolute() or ".." in supplied.parts:
        raise ValueError("recovery_graph_inventory_explicit_root_required")
    root = _path(supplied)
    if not root.is_dir():
        raise ValueError("recovery_graph_inventory_existing_root_required")
    if connection.execute("SELECT type FROM sqlite_schema WHERE name='boards'").fetchall() != [("table",)]:
        raise ValueError("recovery_graph_inventory_boards_table_required")
    columns = connection.execute("PRAGMA table_info(boards)").fetchall()
    if [column[1] for column in columns if column[5]] != ["id"]:
        raise ValueError("recovery_graph_inventory_board_identity_schema_drift")
    remaining = max_entries

    def count_entry():
        nonlocal remaining
        remaining -= 1
        if remaining < 0:
            raise ValueError("recovery_graph_inventory_entry_limit")

    board_ids = []
    for row in connection.execute("SELECT id FROM boards ORDER BY id"):
        count_entry()
        if type(row[0]) is not str or not row[0] or len(row[0]) > 256:
            raise ValueError("recovery_graph_inventory_board_id_invalid")
        board_ids.append(row[0])
    store = CommunityGraphBackendBindingStore(root)
    issues, routes, inactive, other = [], [], [], []

    def entries(directory):
        _path(directory)
        if not directory.exists():
            return ()
        if not directory.is_dir():
            raise ValueError("recovery_graph_inventory_expected_directory: " + str(directory))
        observed = []
        for entry in directory.iterdir():
            count_entry()
            _path(entry)
            observed.append(entry)
        return tuple(sorted(observed, key=lambda path: path.name))

    def relative(path):
        return path.relative_to(root).as_posix()

    for entry in entries(root):
        if entry.name == BINDING_PUBLICATION_MUTEX_FILENAME and (not entry.is_file() or entry.stat().st_size != 0):
            raise ValueError("recovery_graph_inventory_publication_mutex_path_occupied")
        if entry.name not in {"boards", "global", ".okto-pulse-serve.lock.acquire", BINDING_PUBLICATION_MUTEX_FILENAME}:
            other.append(relative(entry))
    known_boards = set(board_ids)
    for entry in entries(root / "boards"):
        if entry.name not in known_boards or not entry.is_dir():
            issues.append("orphan_or_invalid_board_storage:" + relative(entry))

    def inspect(scope, board_id):
        # Reuse the binding store's portable-ID validation before constructing
        # any Board path. No unknown ID becomes a path segment implicitly.
        directory = (store.board_grafx_path(board_id, "probe").parent.parent
                     if scope == "board" else root / "global")
        children = entries(directory)
        binding_path = directory / BOARD_BINDING_FILENAME
        binding_present = binding_path.exists()
        generations = entries(directory / "grafx")
        for generation in generations:
            if not generation.is_dir():
                issues.append("invalid_generation_entry:" + relative(generation))
        extras = [entry for entry in children if entry.name not in {
            BOARD_BINDING_FILENAME, BOARD_BINDING_FILENAME + ".lock", "grafx",
            ".graph_route_initialization.lock",
        }]
        other.extend(relative(entry) for entry in extras)
        if not binding_present:
            state = "binding_absent_storage_present" if generations or extras else "binding_absent_storage_absent"
            routes.append(RecoveryGraphRoute(scope, board_id, state))
            inactive.extend(relative(path) for path in generations)
            if generations or extras:
                issues.append("unbound_storage:" + relative(directory))
            return
        binding = (store.inspect_board_binding(board_id) if scope == "board" else store.inspect_global_binding())
        identity_hash = None
        if binding.backend != "grafx":
            issues.append("unsupported_bound_backend:" + relative(binding_path))
        elif not binding.physical_path.is_dir():
            issues.append("active_generation_missing:" + relative(binding.physical_path))
        elif not _path(binding.physical_path / "grafx.meta").is_file():
            issues.append("active_generation_identity_missing:" + relative(binding.physical_path))
        else:
            identity_file = _path(binding.physical_path / "grafx.meta")
            if identity_file.stat().st_size > 1024 * 1024:
                raise ValueError("recovery_graph_inventory_identity_file_limit")
            identity_hash = _digest(identity_file)
        routes.append(RecoveryGraphRoute(scope, board_id, "bound", binding.generation,
            str(binding.physical_path), binding.page_size, binding.binding_sha256, identity_hash))
        inactive.extend(relative(path) for path in generations if path != binding.physical_path)

    for board_id in board_ids:
        inspect("board", board_id)
    inspect("global_discovery", None)
    return RecoveryGraphInventory(str(root), tuple(board_ids), tuple(routes),
        tuple(sorted(inactive)), tuple(sorted(other)), tuple(sorted(issues)))


def require_recovery_graph_selection(inventory: RecoveryGraphInventory, selected: tuple) -> None:
    """Match every bound route to exactly one (scope, Board, path, page size)."""
    inventory.require_resolved_routes()
    expected = {(route.scope, route.board_id, route.physical_path, route.page_size)
                for route in inventory.routes if route.state == "bound"}
    if len(set(selected)) != len(selected) or set(selected) != expected:
        raise ValueError("recovery_graph_inventory_selection_mismatch")


def recovery_graph_inventory_from_manifest(value: dict) -> RecoveryGraphInventory:
    """Validate recorded routing coverage without reading any live source path."""
    if type(value) is not dict or set(value) != {
        "kg_base_dir", "board_ids", "routes", "unselected_generation_paths", "other_storage_paths", "issues",
    }:
        raise ValueError("recovery_graph_inventory_manifest_invalid")
    if type(value["kg_base_dir"]) is not str or not value["kg_base_dir"]:
        raise ValueError("recovery_graph_inventory_manifest_root_invalid")
    for key in ("board_ids", "unselected_generation_paths", "other_storage_paths", "issues"):
        items = value[key]
        if type(items) is not list or any(type(item) is not str or not item for item in items) or items != sorted(set(items)):
            raise ValueError("recovery_graph_inventory_manifest_list_invalid")
    if type(value["routes"]) is not list:
        raise ValueError("recovery_graph_inventory_manifest_routes_invalid")
    routes = tuple(RecoveryGraphRoute(**route) for route in value["routes"])
    expected = [("board", board_id) for board_id in value["board_ids"]] + [("global_discovery", None)]
    if [(route.scope, route.board_id) for route in routes] != expected:
        raise ValueError("recovery_graph_inventory_manifest_scope_mismatch")
    for route in routes:
        fields = (route.generation, route.physical_path, route.page_size, route.binding_sha256, route.identity_file_sha256)
        if route.state == "bound":
            if (any(type(item) is not str or not item for item in (route.generation, route.physical_path, route.binding_sha256, route.identity_file_sha256))
                or type(route.page_size) is not int or route.page_size <= 0):
                raise ValueError("recovery_graph_inventory_manifest_binding_invalid")
        elif route.state != "binding_absent_storage_absent" or any(item is not None for item in fields):
            raise ValueError("recovery_graph_inventory_manifest_state_invalid")
    inventory = RecoveryGraphInventory(value["kg_base_dir"], tuple(value["board_ids"]), routes,
        tuple(value["unselected_generation_paths"]), tuple(value["other_storage_paths"]), tuple(value["issues"]))
    inventory.require_resolved_routes()
    return inventory
