"""Physical JSON/source-ref census inside the migration's existing snapshot.

Reads declared JSON (including legacy TEXT for current ORM JSON fields) and
explicit source-ref columns. It does not guess ownership through arbitrary FKs,
parse prose as JSON, transform signed content or expose privileged diagnostics.
"""

from dataclasses import dataclass
import json

from sqlalchemy import JSON, inspect, text

from okto_pulse.core.domain.sprint_retirement_embedded import find_embedded_sprint_references
from okto_pulse.community.adapters.sqlalchemy_models import Base


_MAX_CELL_BYTES = 1024 * 1024
_MAX_TOTAL_BYTES = 64 * 1024 * 1024
_TEXT_REFS = {"source_ref", "membership_source_ref", "evidence_ref"}
_PARENTS = {
    **{f"{kind}_{suffix}": (f"{kind}s", ((f"{kind}_id", "id"),))
       for kind in ("ideation", "refinement", "spec") for suffix in ("history", "knowledge_bases", "qa_items")},
    "ideation_snapshots": ("ideations", (("ideation_id", "id"),)),
    "refinement_snapshots": ("refinements", (("refinement_id", "id"),)),
    "sprint_history": ("sprints", (("sprint_id", "id"),)),
    "sprint_qa_items": ("sprints", (("sprint_id", "id"),)),
    "comments": ("cards", (("card_id", "id"),)),
    "kg_cognitive_source_revisions": ("kg_cognitive_sources", (("cognitive_source_id", "id"),)),
    "knowledge_propagation_assignments": ("knowledge_propagation_scopes", (("scope_id", "id"),)),
    "knowledge_propagation_snapshots": ("knowledge_propagation_scopes", (("scope_id", "id"),)),
    "quality_proposed_questions": ("quality_assessment_receipts", (("receipt_id", "id"),)),
    "research_decision_outbox": ("domain_events", (("event_id", "id"),)),
    "project_structure_mutation_receipts": ("specs", (("spec_id", "id"),)),
    "architecture_design_versions": ("architecture_designs", (("design_id", "id"),)),
    "architecture_candidate_decisions": ("architecture_classification_receipts", (
        ("spec_id", "spec_id"), ("idempotency_key", "idempotency_key"))),
}


@dataclass(frozen=True, slots=True)
class SprintEmbeddedReference:
    table: str
    key: tuple[tuple[str, object], ...]
    column: str
    path: tuple[str | int, ...]
    sprint_id: str
    owner_board_id: str | None
    reference_board_id: str | None
    scope_state: str
    form: str


@dataclass(frozen=True, slots=True)
class SprintEmbeddedInventory:
    scanned_counts: tuple[tuple[str, int], ...]
    references: tuple[SprintEmbeddedReference, ...]

    def require_resolved_scopes(self):
        unresolved = tuple(r for r in self.references if r.scope_state not in {"current_source", "historical_source_absent"})
        if unresolved:
            error = SprintEmbeddedInspectionError("sprint_embedded_scope_review")
            error.references = unresolved
            raise error


class SprintEmbeddedInspectionError(RuntimeError):
    references: tuple[SprintEmbeddedReference, ...] = ()


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("non_finite_json_number")


def inspect_sprint_embedded_references(connection, *, remaining_rows: int, sprint_boards: dict[str, str]):
    schema = inspect(connection)
    quote = connection.dialect.identifier_preparer.quote
    known_json = {(table.name, column.name) for table in Base.metadata.tables.values()
        for column in table.columns if isinstance(column.type, JSON)}
    counts, references = {}, []
    consumed = total_bytes = 0
    validated_parents = set()
    for table in sorted(schema.get_table_names()):
        columns = schema.get_columns(table)
        names = {c["name"] for c in columns}
        fields = [(c["name"], isinstance(c["type"], JSON) or (table, c["name"]) in known_json)
            for c in columns if isinstance(c["type"], JSON) or (table, c["name"]) in known_json or c["name"] in _TEXT_REFS]
        if not fields:
            continue
        key_columns = tuple(schema.get_pk_constraint(table)["constrained_columns"])
        if not key_columns:
            raise SprintEmbeddedInspectionError(f"sprint_embedded_key_missing:{table}")
        owner, join = "NULL", ""
        if table == "boards":
            owner = "r.id"
        elif "board_id" in names:
            owner = "r.board_id"
        elif table == "guideline_import_binding_candidates":
            owner = "r.target_board_id"
        elif table in _PARENTS:
            parent, pairs = _PARENTS[table]
            parent_names = {c["name"] for c in schema.get_columns(parent)}
            if not {local for local, _ in pairs} <= names or not {"board_id", *(remote for _, remote in pairs)} <= parent_names:
                raise SprintEmbeddedInspectionError(f"sprint_embedded_owner_schema_invalid:{table}")
            join = f"LEFT JOIN {quote(parent)} parent ON " + " AND ".join(f"r.{quote(local)}=parent.{quote(remote)}" for local, remote in pairs)
            owner = "parent.board_id"
        keys_sql = ",".join(f"r.{quote(key)} AS {quote('pk_' + str(index))}" for index, key in enumerate(key_columns))
        for column, is_json in fields:
            label = f"{table}.{column}"
            counts[label] = 0
            field = f"r.{quote(column)}"
            # SQLite BLOB length bounds UTF-8 bytes before materialization; the
            # migration already limits this internal path to supported schemas.
            size = f"length(CAST({field} AS BLOB))" if connection.dialect.name == "sqlite" else f"octet_length(CAST({field} AS TEXT))"
            query = f"""SELECT {keys_sql}, {owner} AS owner_board, board.id AS owner_exists,
                {size} AS cell_bytes, CASE WHEN {size} <= :byte_limit THEN CAST({field} AS TEXT) END AS content
                FROM {quote(table)} r {join} LEFT JOIN boards board ON board.id={owner}
                WHERE {field} IS NOT NULL ORDER BY {','.join('r.' + quote(key) for key in key_columns)} LIMIT :row_limit"""
            with connection.execute(text(query).execution_options(stream_results=True, yield_per=16),
                    {"byte_limit": _MAX_CELL_BYTES, "row_limit": remaining_rows - consumed + 1}) as rows:
                for row in rows.mappings():
                    consumed += 1
                    counts[label] += 1
                    total_bytes += row["cell_bytes"]
                    if consumed > remaining_rows or row["cell_bytes"] > _MAX_CELL_BYTES or total_bytes > _MAX_TOTAL_BYTES:
                        raise SprintEmbeddedInspectionError(f"sprint_embedded_inspection_limit:{label}")
                    key = tuple((name, row[f"pk_{index}"]) for index, name in enumerate(key_columns))
                    try:
                        value = json.loads(row["content"], object_pairs_hook=_object, parse_constant=_constant) if is_json else row["content"]
                        matches = find_embedded_sprint_references(value, field_name=column)
                    except (ValueError, TypeError, RecursionError) as error:
                        raise SprintEmbeddedInspectionError(f"sprint_embedded_payload_invalid:{label}:{key!r}") from error
                    for match in matches:
                        if table in _PARENTS and table not in validated_parents:
                            parent, pairs = _PARENTS[table]
                            constraints = schema.get_foreign_keys(table)
                            if not any(fk["referred_table"] == parent and
                                    set(zip(fk["constrained_columns"], fk["referred_columns"], strict=True)) == set(pairs)
                                    for fk in constraints):
                                raise SprintEmbeddedInspectionError(f"sprint_embedded_owner_fk_invalid:{table}")
                            validated_parents.add(table)
                        board_id = row["owner_board"]
                        target_board = match.board_hint or board_id
                        state = "current_source" if match.sprint_id in sprint_boards else "historical_source_absent"
                        if owner == "NULL":
                            state = "owner_scope_unclassified"
                        elif row["owner_exists"] is None:
                            state = "owner_scope_missing"
                        elif target_board != board_id:
                            state = "reference_board_requires_review"
                        elif match.sprint_id in sprint_boards and sprint_boards[match.sprint_id] != target_board:
                            state = "cross_board_reference"
                        references.append(SprintEmbeddedReference(table, key, column, match.path,
                            match.sprint_id, board_id, target_board, state, match.form))
    return SprintEmbeddedInventory(tuple(sorted(counts.items())), tuple(references))
