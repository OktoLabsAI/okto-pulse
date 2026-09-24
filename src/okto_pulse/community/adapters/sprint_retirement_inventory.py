"""Internal F2 relational preflight, not a migration or a public maintenance API.

Read all Boards in one database snapshot and fail closed on incomplete inspection.
The returned IDs are privileged migration diagnostics, never an ACL-filtered
product response. A cutover must repeat these checks under its write fence after
archival; this read does not authorize writes or certify archive/KG/job coverage.
Historical baseline members remain opaque history, not current Card foreign keys.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from okto_pulse.community.adapters.sprint_retirement_work import (
    SprintWorkInventory,
    inspect_sprint_retirement_work,
)
from okto_pulse.community.adapters.sprint_retirement_references import (
    SprintReferenceInventory,
    inspect_sprint_historical_references,
)
from okto_pulse.community.adapters.sprint_retirement_embedded import (
    SprintEmbeddedInventory,
    inspect_sprint_embedded_references,
)


_OWNED = frozenset({"sprints", "sprint_history", "sprint_qa_items", "sprint_activation_baselines"})
_REQUIRED = {
    "boards": {"id"},
    "specs": {"id", "board_id"},
    "sprints": {"id", "board_id", "spec_id", "origin_sprint_id", "origin_bug_id"},
    "cards": {"id", "board_id", "spec_id", "sprint_id"},
    "sprint_history": {"id", "sprint_id"},
    "sprint_qa_items": {"id", "sprint_id"},
    "sprint_activation_baselines": {"baseline_ref", "sprint_id", "board_id", "spec_id"},
}
_KNOWN_REFERENCES = {
    ("cards", ("sprint_id",), "sprints", ("id",)),
    ("sprints", ("origin_sprint_id",), "sprints", ("id",)),
    ("sprint_history", ("sprint_id",), "sprints", ("id",)),
    ("sprint_qa_items", ("sprint_id",), "sprints", ("id",)),
    ("sprint_activation_baselines", ("sprint_id",), "sprints", ("id",)),
}


class SprintRetirementInspectionError(RuntimeError):
    """Inspection was incomplete; no empty or successful inventory is returned."""


@dataclass(frozen=True, slots=True)
class SprintRelationViolation:
    relation: str
    row_id: str
    target_id: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class SprintRelationalInventory:
    """Counts are physical rows; violations are complete, never a sample.

    Deliberately no `ready` flag: even zero violations says nothing about archive
    integrity, pending findings, polymorphic references, queues or graph sources.
    """

    counts: tuple[tuple[str, int], ...]
    violations: tuple[SprintRelationViolation, ...]
    work: SprintWorkInventory
    historical_references: SprintReferenceInventory
    embedded_references: SprintEmbeddedInventory

    def require_valid_relations(self) -> None:
        if self.violations:
            raise SprintRetirementRelationsInvalid(self)


class SprintRetirementRelationsInvalid(RuntimeError):
    def __init__(self, inventory: SprintRelationalInventory) -> None:
        self.inventory = inventory
        # IDs remain in structured diagnostics, not an indiscriminate log string.
        super().__init__(f"sprint_retirement_relations_invalid:{len(inventory.violations)}")


def _inspect_snapshot(connection: Connection, *, max_rows: int) -> SprintRelationalInventory:
    schema = inspect(connection)
    tables = set(schema.get_table_names())
    for table, required in _REQUIRED.items():
        if table not in tables:
            raise SprintRetirementInspectionError(f"sprint_retirement_missing_table:{table}")
        missing = required - {column["name"] for column in schema.get_columns(table)}
        if missing:
            raise SprintRetirementInspectionError(
                f"sprint_retirement_missing_columns:{table}:{','.join(sorted(missing))}"
            )
    # Inspect the physical schema, not only today's ORM metadata. An extension
    # referencing a retired table must be classified before a destructive cutover.
    for table in sorted(tables):
        if table not in _OWNED:
            for column in schema.get_columns(table):
                if "sprint" in column["name"].lower() and (table, column["name"]) != ("cards", "sprint_id"):
                    raise SprintRetirementInspectionError(
                        f"sprint_retirement_unclassified_column:{table}.{column['name']}"
                    )
        for foreign_key in schema.get_foreign_keys(table):
            target = foreign_key["referred_table"]
            if target not in _OWNED:
                continue
            reference = (table, tuple(foreign_key["constrained_columns"]), target,
                         tuple(foreign_key["referred_columns"]))
            if reference not in _KNOWN_REFERENCES:
                raise SprintRetirementInspectionError(f"sprint_retirement_unclassified_reference:{reference!r}")

    counts: dict[str, int] = {}
    violations: list[SprintRelationViolation] = []
    sprint_boards: dict[str, str] = {}
    consumed = 0

    def rows(name: str, query: str):
        nonlocal consumed
        counts[name] = 0
        statement = text(query + " LIMIT :inventory_limit").execution_options(stream_results=True, yield_per=16)
        with connection.execute(statement, {"inventory_limit": max_rows - consumed + 1}) as result:
            for row in result.mappings():
                consumed += 1
                if consumed > max_rows:
                    raise SprintRetirementInspectionError(f"sprint_retirement_row_limit:{max_rows}")
                counts[name] += 1
                yield row

    def check(row, *, relation: str, target_id, actual_id, board_id=None, actual_board=None):
        if actual_id is None:
            violations.append(SprintRelationViolation(relation, row["id"], target_id, "orphan"))
        elif board_id is not None and board_id != actual_board:
            violations.append(SprintRelationViolation(relation, row["id"], target_id, "cross_board"))

    for row in rows("sprints", """
        SELECT s.id, s.board_id, s.spec_id, s.origin_sprint_id, s.origin_bug_id,
               b.id AS board_row, sp.id AS spec_row, sp.board_id AS spec_board,
               origin.id AS origin_row, origin.board_id AS origin_board,
               bug.id AS bug_row, bug.board_id AS bug_board
        FROM sprints s LEFT JOIN boards b ON b.id = s.board_id
        LEFT JOIN specs sp ON sp.id = s.spec_id
        LEFT JOIN sprints origin ON origin.id = s.origin_sprint_id
        LEFT JOIN cards bug ON bug.id = s.origin_bug_id ORDER BY s.id
    """):
        sprint_boards[row["id"]] = row["board_id"]
        check(row, relation="sprints.board_id", target_id=row["board_id"], actual_id=row["board_row"])
        check(row, relation="sprints.spec_id", target_id=row["spec_id"], actual_id=row["spec_row"],
              board_id=row["board_id"], actual_board=row["spec_board"])
        for column, prefix in (("origin_sprint_id", "origin"), ("origin_bug_id", "bug")):
            if row[column] is not None:
                check(row, relation=f"sprints.{column}", target_id=row[column], actual_id=row[f"{prefix}_row"],
                      board_id=row["board_id"], actual_board=row[f"{prefix}_board"])

    for row in rows("linked_cards", """
        SELECT c.id, c.board_id, c.spec_id, c.sprint_id, b.id AS board_row,
               s.id AS sprint_row, s.board_id AS sprint_board,
               sp.id AS spec_row, sp.board_id AS spec_board
        FROM cards c LEFT JOIN sprints s ON s.id = c.sprint_id
        LEFT JOIN boards b ON b.id = c.board_id
        LEFT JOIN specs sp ON sp.id = c.spec_id
        WHERE c.sprint_id IS NOT NULL ORDER BY c.id
    """):
        check(row, relation="cards.board_id", target_id=row["board_id"], actual_id=row["board_row"])
        check(row, relation="cards.sprint_id", target_id=row["sprint_id"], actual_id=row["sprint_row"],
              board_id=row["board_id"], actual_board=row["sprint_board"])
        if row["spec_id"] is not None:
            check(row, relation="cards.spec_id", target_id=row["spec_id"], actual_id=row["spec_row"],
                  board_id=row["board_id"], actual_board=row["spec_board"])
        # Do not require c.spec_id == s.spec_id: cross-Spec regression Test Cards
        # are legitimate. Amendment/scenario authorization remains its own gate.

    for table in ("sprint_history", "sprint_qa_items", "sprint_activation_baselines"):
        baseline = table == "sprint_activation_baselines"
        identity = "baseline_ref" if baseline else "id"
        extra = ", child.board_id, child.spec_id, s.spec_id AS sprint_spec" if baseline else ""
        for row in rows(table, f"""
            SELECT child.{identity} AS id, child.sprint_id, s.id AS sprint_row,
                   s.board_id AS sprint_board {extra}
            FROM {table} child LEFT JOIN sprints s ON s.id = child.sprint_id
            ORDER BY child.{identity}
        """):
            check(row, relation=f"{table}.sprint_id", target_id=row["sprint_id"], actual_id=row["sprint_row"],
                  board_id=row["board_id"] if baseline else None, actual_board=row["sprint_board"])
            if baseline and row["sprint_row"] is not None and row["spec_id"] != row["sprint_spec"]:
                violations.append(SprintRelationViolation(f"{table}.spec_id", row["id"], row["spec_id"], "scope_mismatch"))
    work = inspect_sprint_retirement_work(connection, remaining_rows=max_rows - consumed, sprint_boards=sprint_boards)
    references = inspect_sprint_historical_references(connection,
        remaining_rows=max_rows - consumed - sum(count for _, count in work.scanned_counts))
    embedded = inspect_sprint_embedded_references(connection, sprint_boards=sprint_boards,
        remaining_rows=max_rows - consumed - sum(count for _, count in work.scanned_counts) - sum(count for _, count in references.counts))
    return SprintRelationalInventory(tuple(sorted(counts.items())), tuple(violations), work, references, embedded)


async def read_sprint_retirement_inventory(
    engine: AsyncEngine, *, max_rows: int = 100_000,
) -> SprintRelationalInventory:
    """Read-only internal preparation; does not open paths or user runtime state.

    SQLite's legacy driver does not start a snapshot for SELECT after a Python
    begin(), so issue BEGIN explicitly. The caller must supply an explicitly
    selected Community SQLite engine.
    """
    if type(max_rows) is not int or max_rows < 1:
        raise ValueError("sprint_retirement_row_limit_invalid")
    if engine.dialect.name != "sqlite":
        raise SprintRetirementInspectionError("sprint_retirement_backend_unsupported")
    async with engine.connect() as connection:
        try:
            await connection.exec_driver_sql("BEGIN")
            return await connection.run_sync(lambda sync: _inspect_snapshot(sync, max_rows=max_rows))
        finally:
            await connection.rollback()
