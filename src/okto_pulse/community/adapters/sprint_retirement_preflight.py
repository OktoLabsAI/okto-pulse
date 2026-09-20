"""Snapshot preflight before any Card transform, not a complete cutover receipt.

Source contents never appear in diagnostic errors. An empty context inventory
does not certify graph cleanup, stopped workers, schema parity or rollback.
"""

from dataclasses import dataclass
import hashlib
import json

from sqlalchemy import inspect, text

from okto_pulse.core.ports.retirement_context import inspect_retirement_context
from okto_pulse.community.adapters.sprint_retirement_archive import _cell, _encode
from okto_pulse.community.adapters.sprint_retirement_embedded import _constant, _object
from okto_pulse.community.adapters.sprint_retirement_inventory import SprintRelationalInventory, _inspect_snapshot
from okto_pulse.community.adapters.sqlalchemy_models import Base

_MAX_ROW_BYTES = 1024 * 1024
_MAX_TOTAL_BYTES = 64 * 1024 * 1024
_SOURCES = (
    ("sprints", "sprint", ("id", "board_id", "spec_id", "description", "objective", "expected_outcome", "evaluations"), ("evaluations",)),
    ("sprint_qa_items", "qa", ("id", "sprint_id", "question", "question_type", "choices", "allow_free_text",
        "answer", "selected", "asked_by", "answered_by", "created_at", "answered_at"), ("choices", "selected")),
    ("sprint_history", "history", ("id", "sprint_id", "action", "actor_type", "actor_id", "actor_name",
        "changes", "summary", "version", "created_at"), ("changes",)),
)
# These references are mechanics/history already inventoried in their own
# retirement steps. Every other polymorphic source needs explicit disposition;
# a receipt/waiver/evaluation must not silently become authority for a new target.
_MECHANICAL_REFERENCE_TABLES = frozenset({
    "consolidation_queue", "artifact_deletion_tombstones", "canonical_debt", "consolidation_dead_letter",
    "consolidation_audit", "exact_rebuild_consolidation_ack_journal", "global_discovery_delivery_ledger",
    "kg_takedown_state_events", "agent_seen_items",
})


@dataclass(frozen=True, slots=True)
class SprintContextCandidate:
    table: str
    key: tuple[tuple[str, object], ...]
    path: tuple[str | int, ...]
    board_id: str | None
    origin_id: str | None
    source_spec_id: str | None
    source_sha256: str
    reason: str


class SprintContextDispositionRequired(ValueError):
    def __init__(self, candidates):
        self.candidates = tuple(candidates)
        super().__init__(f"sprint_retirement_context_requires_disposition:{len(self.candidates)}")


@dataclass(frozen=True, slots=True)
class SprintPretransformInventory:
    relational: SprintRelationalInventory
    context_scanned_counts: tuple[tuple[str, int], ...]
    context_candidates: tuple[SprintContextCandidate, ...]

    def require_resolved_pretransform(self):
        self.relational.require_valid_relations()
        self.relational.work.require_classified_work()
        self.relational.historical_references.require_resolved_scopes()
        self.relational.embedded_references.require_resolved_scopes()
        if self.context_candidates:
            raise SprintContextDispositionRequired(self.context_candidates)


def inspect_sprint_pretransform(connection, *, max_rows=100_000):
    if connection.dialect.name != "sqlite":
        raise ValueError("sprint_pretransform_backend_unsupported")
    if type(max_rows) is not int or max_rows < 1:
        raise ValueError("sprint_retirement_row_limit_invalid")
    inventory = _inspect_snapshot(connection, max_rows=max_rows)
    inventory.require_valid_relations()
    inventory.historical_references.require_resolved_scopes()
    inventory.embedded_references.require_resolved_scopes()
    consumed = sum(count for group in (inventory.counts, inventory.work.scanned_counts,
        inventory.historical_references.counts, inventory.embedded_references.scanned_counts) for _, count in group)
    schema = inspect(connection)
    counts, candidates, owners = {}, [], {}
    total_bytes = 0
    quote = connection.dialect.identifier_preparer.quote
    for table, kind, required, json_columns in _SOURCES:
        columns = tuple(column["name"] for column in schema.get_columns(table))
        if not set(required) <= set(columns) or set(columns) - set(Base.metadata.tables[table].columns.keys()):
            raise ValueError(f"sprint_context_schema_invalid:{table}")
        # Bind the disposition to every physical source field, including author,
        # dates and version. Unknown extension fields require investigation.
        quoted = [quote(column) for column in columns]
        size = "+".join(f"coalesce(length(CAST({column} AS BLOB)),0)" for column in quoted)
        projections = ",".join(f"CASE WHEN {size}<=:cap THEN {column} END AS {column}" for column in quoted)
        counts[table] = 0
        query = text(f'SELECT {size} AS row_bytes,{projections} FROM "{table}" ORDER BY id LIMIT :remaining')
        with connection.execute(query.execution_options(stream_results=True, yield_per=16),
                {"cap": _MAX_ROW_BYTES, "remaining": max_rows - consumed + 1}) as rows:
            for row in rows.mappings():
                consumed += 1
                counts[table] += 1
                total_bytes += row["row_bytes"]
                if consumed > max_rows or row["row_bytes"] > _MAX_ROW_BYTES or total_bytes > _MAX_TOTAL_BYTES:
                    raise ValueError("sprint_retirement_context_limit")
                facts = {column: row[column] for column in columns}
                digest = hashlib.sha256(_encode([(name, _cell(facts[name])) for name in columns])).hexdigest()
                if kind == "sprint":
                    origin = facts["id"]
                    owners[origin] = (facts["board_id"], facts["spec_id"])
                else:
                    origin = facts["sprint_id"]
                for name in json_columns:
                    if facts[name] is not None:
                        try:
                            facts[name] = json.loads(facts[name], object_pairs_hook=_object, parse_constant=_constant)
                        except (ValueError, TypeError, RecursionError) as error:
                            raise ValueError("sprint_retirement_context_json_invalid") from error
                for concern in inspect_retirement_context(kind, facts):
                    board, spec = owners[origin]
                    candidates.append(SprintContextCandidate(table, (("id", facts["id"]),), concern.path,
                        board, origin, spec, digest, concern.reason))
    # Bound governance references are already enumerated in this same snapshot.
    # Keep exact keys/roles; no status/waiver/score can silently transfer their
    # normative meaning to a Spec/Card. Their full bytes remain in the archive.
    hashes = {}

    def source_hash(table, key):
        nonlocal consumed, total_bytes
        identity = (table, key)
        if identity not in hashes:
            names = tuple(column["name"] for column in schema.get_columns(table))
            columns = tuple(quote(name) for name in names)
            predicates = " AND ".join(f"{quote(name)} IS :p{index}" for index, (name, _) in enumerate(key))
            params = {f"p{index}": value for index, (_, value) in enumerate(key)}
            size_sql = "+".join(f"coalesce(length(CAST({name} AS BLOB)),0)" for name in columns)
            size = connection.execute(text(f"SELECT {size_sql} FROM {quote(table)} WHERE {predicates}"), params).scalar_one()
            consumed += 1
            counts[table] = counts.get(table, 0) + 1
            total_bytes += size
            if consumed > max_rows or size > _MAX_ROW_BYTES or total_bytes > _MAX_TOTAL_BYTES:
                raise ValueError("sprint_retirement_context_limit")
            row = connection.execute(text(f'SELECT {",".join(columns)} FROM {quote(table)} WHERE {predicates}'), params).one()
            hashes[identity] = hashlib.sha256(_encode([(name, _cell(value)) for name, value in zip(names, row, strict=True)])).hexdigest()
        return hashes[identity]

    for reference in inventory.historical_references.references:
        if reference.table not in _MECHANICAL_REFERENCE_TABLES:
            candidates.append(SprintContextCandidate(reference.table, reference.key, (reference.role,),
                reference.owner_board_id, reference.sprint_id,
                owners.get(reference.sprint_id, (None, None))[1],
                source_hash(reference.table, reference.key), "bound_reference_requires_disposition"))
    handled = _MECHANICAL_REFERENCE_TABLES | {table for table, *_ in _SOURCES} | {
        "domain_events", "domain_event_handler_executions", "sprint_activation_baselines"}
    for reference in inventory.embedded_references.references:
        if reference.table not in handled:
            candidates.append(SprintContextCandidate(reference.table, reference.key, (reference.column, *reference.path),
                reference.owner_board_id, reference.sprint_id,
                owners.get(reference.sprint_id, (None, None))[1],
                source_hash(reference.table, reference.key), "embedded_reference_requires_disposition"))
    return SprintPretransformInventory(inventory, tuple(sorted(counts.items())), tuple(candidates))


async def read_sprint_pretransform(engine, *, max_rows=100_000):
    if engine.dialect.name != "sqlite":
        raise ValueError("sprint_pretransform_backend_unsupported")
    async with engine.connect() as connection:
        try:
            await connection.exec_driver_sql("BEGIN")
            return await connection.run_sync(lambda sync: inspect_sprint_pretransform(sync, max_rows=max_rows))
        finally:
            await connection.rollback()
