"""Sealed input and routing proof for offline graph/outbox retirement."""

from contextlib import closing
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3

from sqlalchemy import create_engine

from okto_pulse.core.ports.global_retirement_graph import GlobalGraphRetirementPlan, GlobalRetirementSourceFacts
from okto_pulse.core.ports.retirement_graph import GraphRetirementPlan, graph_retirement_fingerprint
from .global_outbox_retirement import _derive, _rows, _snapshot, prepare_outbox_retirement
from .grafx_global_retirement import prepare_global_graph_retirement
from .grafx_sprint_retirement import prepare_sprint_graph_retirement
from .joint_recovery_snapshot import RecoveryGraph, _explicit_path, _stamp
from .logical_transfer_factories import make_grafx_logical_source
from .recovery_graph_inventory import read_recovery_graph_inventory, require_recovery_graph_selection
from .sprint_retirement_archive import _encode, verify_historical_archive

_FORMAT = "retirement-materialization-plan/v1"
_KEYS = {"format", "board_plans", "global_plan", "archived_origins", "original_outbox", "selected_ids", "after_sha256"}
_MAX_BYTES = 64 * 1024 * 1024


def encode_materialization_plan(plan):
    result = {"format": _FORMAT, "board_plans": [asdict(item) for item in plan.board_plans],
        "global_plan": asdict(plan.graph_plan) if plan.graph_plan is not None else None,
        "archived_origins": plan.archived_origins, "original_outbox": plan.original.decode("utf-8"),
        "selected_ids": plan.selected_ids, "after_sha256": plan.after_sha256}
    raw = _encode(result)
    if len(raw) > _MAX_BYTES:
        raise ValueError("retirement_materialization_plan_limit")
    return json.loads(raw)


def decode_materialization_plan(document):
    try:
        if (type(document) is not dict or set(document) != _KEYS or document["format"] != _FORMAT
                or len(_encode(document)) > _MAX_BYTES or type(document["board_plans"]) is not list
                or len(document["board_plans"]) > 256):
            raise ValueError
        def board(value):
            return GraphRetirementPlan(**{**value, "node_keys": tuple(tuple(key) for key in value["node_keys"])})
        boards = tuple(board(value) for value in document["board_plans"])
        value = document["global_plan"]
        global_plan = None if value is None else GlobalGraphRetirementPlan(**{**value,
            "sources": tuple(GlobalRetirementSourceFacts(**{**item, "board_plan": board(item["board_plan"]),
                "removed_node_ids": tuple(item["removed_node_ids"])}) for item in value["sources"]),
            "digest_ids": tuple(value["digest_ids"]), "board_counts": tuple(tuple(item) for item in value["board_counts"])})
        plan = _derive(global_plan, tuple((owner, tuple(ids)) for owner, ids in document["archived_origins"]),
            document["original_outbox"].encode("utf-8"), boards)
        if encode_materialization_plan(plan) != document:
            raise ValueError
        return plan
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError) as error:
        raise ValueError("retirement_materialization_plan_invalid") from error


def graph_handles(graphs):
    if (type(graphs) is not tuple or len(graphs) > 256 or any(not isinstance(item, RecoveryGraph) for item in graphs)
            or len({(item.scope, item.board_id) for item in graphs}) != len(graphs)):
        raise ValueError("retirement_materialization_graph_selection_invalid")
    boards = {item.board_id: item.database for item in graphs if item.scope == "board"}
    global_db = next((item.database for item in graphs if item.scope == "global_discovery"), None)
    return boards, global_db


def require_materialization_bindings(source, kg_root, graphs, manifest):
    """Called while the coordinator holds the binding publication mutex."""
    graph_handles(graphs)
    with closing(sqlite3.connect(_explicit_path(source).as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("BEGIN")
        current = read_recovery_graph_inventory(connection, kg_root)
    observed = current.as_manifest()
    original = json.loads(_encode(manifest["routing_inventory"]))
    # Native writes may change identity-file bytes. Physical UUID is checked
    # below; routing, generation, page size and the binding digest cannot move.
    for value in (observed, original):
        for route in value["routes"]:
            route["identity_file_sha256"] = None
    if observed != original:
        raise ValueError("retirement_materialization_routing_mismatch")
    require_recovery_graph_selection(current, tuple((item.scope, item.board_id,
        str(_explicit_path(Path(item.database.path))), item.database.identity.page_size) for item in graphs))
    records = {(item["scope"], item["board_id"]): item for item in manifest["graphs"]}
    for graph in graphs:
        record = records.get((graph.scope, graph.board_id))
        if (record is None or str(_explicit_path(Path(graph.database.path))) != record["source_path"]
                or _stamp(graph)["database_uuid"] != record["database_uuid"]):
            raise ValueError("retirement_materialization_physical_identity_mismatch")


def require_materialization_states(plan, graphs, *, original=False, retired=False):
    boards, global_db = graph_handles(graphs)
    if set(boards) != {item.board_id for item in plan.board_plans} or (global_db is None) != (plan.graph_plan is None):
        raise ValueError("retirement_materialization_graph_population_mismatch")
    states = []
    selections = [(boards[item.board_id], "board", item) for item in plan.board_plans]
    if plan.graph_plan is not None:
        selections.append((global_db, "global_discovery", plan.graph_plan))
    for database, scope, item in selections:
        snapshot = make_grafx_logical_source(database, scope=scope).open_snapshot()
        try:
            current = graph_retirement_fingerprint(snapshot, scope=scope)
        finally:
            snapshot.close()
        allowed = {item.before_sha256} if original else {item.after_sha256} if retired else {item.before_sha256, item.after_sha256}
        if current not in allowed:
            raise ValueError("retirement_materialization_graph_state_mismatch")
        states.append(current == item.after_sha256)
    if plan.graph_plan is not None and plan.graph_plan.before_sha256 != plan.graph_plan.after_sha256:
        if states[-1] and not all(states[:-1]):
            raise ValueError("retirement_materialization_stage_order_invalid")
    return all(states)


async def prepare_materialization_plan(engine, storage, references, graphs, backup, manifest):
    boards, global_db = graph_handles(graphs)
    archived = {}
    for reference in references:
        document = await verify_historical_archive(storage, reference)
        section = document["tables"]["sprints"]
        index = next(index for index, column in enumerate(section["columns"]) if column["name"] == "id")
        ids = tuple(sorted(cells[index][1] for cells in section["rows"] if cells[index][0] == "text"))
        if len(ids) != len(section["rows"]) or len(set(ids)) != len(ids):
            raise ValueError("retirement_materialization_archive_identity_invalid")
        archived[reference.board_id] = ids
    plans = tuple(prepare_sprint_graph_retirement(database, board_id=owner,
        archived_origin_ids=frozenset(archived.get(owner, ()))) for owner, database in sorted(boards.items()))
    global_plan = None if global_db is None else prepare_global_graph_retirement(global_db,
        tuple((boards[item.board_id], item) for item in plans))
    plan = await prepare_outbox_retirement(engine, graph_plan=global_plan, board_plans=plans,
        archived_origins=tuple((item.board_id, archived.get(item.board_id, ())) for item in plans))
    original_sql = _explicit_path(backup.directory / "relational" / "database.sqlite3")
    source = create_engine("sqlite://", creator=lambda: sqlite3.connect(original_sql.as_uri() + "?mode=ro&immutable=1", uri=True))
    try:
        with source.connect() as connection:
            if _snapshot(connection) != plan.original:
                raise ValueError("retirement_materialization_original_outbox_mismatch")
    finally:
        source.dispose()
    # An absent Board graph does not prove which nodes of a pending mixed
    # session belonged to Sprint. Refuse that environment rather than discard it.
    for event in _rows(json.loads(plan.original)["global_update_outbox"]):
        if event["processed_at"] is None and archived.get(event["board_id"]) and event["board_id"] not in boards:
            raise ValueError("retirement_materialization_missing_board_source_requires_review")
    if global_db is not None:
        snapshot = make_grafx_logical_source(global_db, scope="global_discovery").open_snapshot()
        try:
            for batch in snapshot.iter_nodes(batch_size=500):
                for node in batch:
                    owner = node.properties.get("board_id")
                    if node.type_name == "DecisionDigest" and archived.get(owner) and owner not in boards:
                        raise ValueError("retirement_materialization_missing_board_source_requires_review")
        finally:
            snapshot.close()
    records = {(item["scope"], item["board_id"]): item for item in manifest["graphs"]}
    for item in plans:
        if item.before_sha256 != records["board", item.board_id]["certificate"]["fingerprint"]:
            raise ValueError("retirement_materialization_original_graph_mismatch")
    if global_plan is not None and global_plan.before_sha256 != records["global_discovery", None]["certificate"]["fingerprint"]:
        raise ValueError("retirement_materialization_original_graph_mismatch")
    encoded = encode_materialization_plan(plan)
    if decode_materialization_plan(encoded) != plan:
        raise ValueError("retirement_materialization_plan_invalid")
    return encoded
