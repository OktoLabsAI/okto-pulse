"""Install explicit context dispositions in the private migration journal.

No source/target entity, approval, finding or permission is rewritten. Bindings
refer to immutable archive sections and must only be read through their ACLs.
This library accepts privileged migration input; it is not a product endpoint.
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
import uuid

from sqlalchemy import LargeBinary, cast, func, insert, select, text

from okto_pulse.core.ports.context_disposition import ContextDispositionPlan, require_context_target_scope
from okto_pulse.community.adapters.historical_archive_grant_installation import _install_historical_archive_grants
from okto_pulse.community.adapters.sprint_retirement_archive import _attach_access, _capture, _cell, _encode, verify_historical_archive
from okto_pulse.community.adapters.sprint_retirement_preflight import SprintContextCandidate, SprintContextDispositionRequired, inspect_sprint_pretransform
from okto_pulse.community.adapters.sqlalchemy_models import DomainEventRow

_EVENT = "migration.context_disposed"
_COMPLETE = "migration.context_disposition_completed"
_COMMITTED = "migration.context_dispositions_committed"
_BINDING = "historical_context.bound"
_FORMAT = "historical-context-disposition/v1"
_NAMESPACE = uuid.UUID("cfb2b577-a87c-5244-bf63-5209c8b7c8bb")
_MAX_BYTES = 64 * 1024 * 1024
_TABLE = DomainEventRow.__table__


def _digest(value):
    return hashlib.sha256(_encode(value)).hexdigest()


def context_candidate_id(candidate):
    return _digest(asdict(candidate))


def _id(migration, kind, identity):
    return str(uuid.uuid5(_NAMESPACE, _encode([migration, kind, identity]).decode("utf-8")))


@dataclass(frozen=True, slots=True)
class ContextDispositionReceipt:
    migration_id: str
    evidence_sha256: str
    candidate_count: int
    binding_count: int

    def __post_init__(self):
        if (type(self.migration_id) is not str or not 1 <= len(self.migration_id) <= 128
                or type(self.evidence_sha256) is not str or re.fullmatch(r"[0-9a-f]{64}", self.evidence_sha256) is None
                or any(type(value) is not int or not 0 <= value <= 100_000 for value in (self.candidate_count, self.binding_count))):
            raise ValueError("context_disposition_receipt_invalid")


def _source_row(document, candidate):
    section = document["tables"][candidate.table]
    names = tuple(column["name"] for column in section["columns"])
    if tuple(section["primary_key"]) != tuple(name for name, _ in candidate.key):
        raise ValueError("context_disposition_source_key_mismatch")
    rows = [cells for cells in section["rows"] if all(
        cells[names.index(name)] == _cell(value) for name, value in candidate.key)]
    if len(rows) != 1 or _digest(list(zip(names, rows[0], strict=True))) != candidate.source_sha256:
        raise ValueError("context_disposition_source_hash_mismatch")
    return dict(zip(names, rows[0], strict=True))


def _selection(candidate, row):
    """Only the four already-authorized projections can back a public binding."""
    identity = dict(candidate.key).get("id")
    if candidate.table == "sprints":
        if candidate.path in (("description",), ("objective",), ("expected_outcome",)):
            return {"section": "content", "record_identity": identity, "record_index": None, "field": candidate.path[0]}
        if len(candidate.path) == 2 and candidate.path[0] == "evaluations" and type(candidate.path[1]) is int:
            value = json.loads(row["evaluations"][1])
            if not 0 <= candidate.path[1] < len(value) or not isinstance(value[candidate.path[1]], dict):
                raise ValueError("context_disposition_selection_invalid")
            return {"section": "evaluations", "record_identity": None, "record_index": candidate.path[1], "field": None}
    if candidate.table in {"sprint_qa_items", "sprint_history"} and candidate.path == ():
        return {"section": "qa" if candidate.table == "sprint_qa_items" else "history",
            "record_identity": identity, "record_index": None, "field": None}
    raise ValueError("context_disposition_projection_unsupported")


def _records(references, documents, candidates, plan):
    population = {context_candidate_id(candidate): candidate for candidate in candidates}
    decisions = {decision.candidate_sha256: decision for decision in plan.decisions}
    if len(population) != len(candidates) or population.keys() != decisions.keys():
        raise ValueError("context_disposition_population_mismatch")
    by_board = {reference.board_id: reference for reference in references}
    rows, size, bindings = [], 0, 0
    for identity, candidate in sorted(population.items()):
        reference = by_board[candidate.board_id]
        row = _source_row(documents[candidate.board_id], candidate)
        decision = decisions[identity]
        selection = _selection(candidate, row) if decision.action == "bind_context" else None
        bindings += len(decision.targets)
        payload = {"format": _FORMAT, "migration_id": plan.migration_id, "decision_reference": plan.decision_reference,
            "archive_id": reference.event_id, "archive_sha256": reference.sha256,
            "candidate": asdict(candidate), "decision": decision.model_dump(mode="json"), "selection": selection}
        record = {"id": _id(plan.migration_id, "candidate", identity), "board_id": candidate.board_id,
            "event_type": _EVENT, "actor_type": "system", "actor_id": None, "payload_json": json.loads(_encode(payload))}
        size += len(_encode(record))
        if size > _MAX_BYTES or bindings > 100_000:
            raise ValueError("context_disposition_limit")
        rows.append(record)
    manifests = []
    for reference in references:
        owned = [row for row in rows if row["board_id"] == reference.board_id]
        manifests.append({"id": _id(plan.migration_id, "complete", reference.board_id), "board_id": reference.board_id,
            "event_type": _COMPLETE, "actor_type": "system", "actor_id": None,
            "payload_json": {"format": _FORMAT, "migration_id": plan.migration_id, "decision_reference": plan.decision_reference,
                "archive_id": reference.event_id, "archive_sha256": reference.sha256,
                "candidate_count": len(owned), "rows_sha256": _digest(owned)}})
    if len(rows) + len(manifests) > 100_000 or size + len(_encode(manifests)) > _MAX_BYTES:
        raise ValueError("context_disposition_limit")
    return ContextDispositionReceipt(plan.migration_id, _digest(manifests), len(candidates), bindings), rows + manifests


async def _journal(connection, migration_id):
    where = (_TABLE.c.event_type.in_((_COMMITTED, _BINDING)), _TABLE.c.payload_json["migration_id"].as_string() == migration_id)
    count, size = (await connection.execute(select(func.count(), func.coalesce(func.sum(
        func.length(cast(_TABLE.c.payload_json, LargeBinary))), 0)).where(*where))).one()
    if count > 100_000 or size > _MAX_BYTES:
        raise ValueError("context_disposition_limit")
    return [{key: value for key, value in row.items() if key != "occurred_at"}
        for row in (await connection.execute(select(_TABLE).where(*where))).mappings()]


async def _documents(connection, storage, references):
    if (not references or len({ref.board_id for ref in references}) != len(references)
            or len({ref.migration_id for ref in references}) != 1 or sum(ref.size for ref in references) > _MAX_BYTES):
        raise ValueError("context_disposition_archive_scope_invalid")
    documents = {}
    for reference in references:
        await _install_historical_archive_grants(connection, storage, reference, require_existing=True)
        documents[reference.board_id] = await verify_historical_archive(storage, reference)
    return documents


async def _require_original_archive(connection, references, migration_id):
    captures = await connection.run_sync(lambda sync: _capture(sync,
        migration_id=migration_id, max_rows=100_000, max_bytes=_MAX_BYTES))
    captures = await _attach_access(connection, captures, max_rows=100_000, max_bytes=_MAX_BYTES)
    if [(identity, board, hashlib.sha256(content).hexdigest(), len(content), counts)
            for identity, board, content, counts in captures] != [
            (ref.event_id, ref.board_id, ref.sha256, ref.size, ref.counts) for ref in references]:
        raise ValueError("context_disposition_archive_changed")


async def _targets(connection, candidates, plan):
    population = {context_candidate_id(candidate): candidate for candidate in candidates}
    snapshots, size = {}, 0
    for decision in plan.decisions:
        for target in decision.targets:
            table = "specs" if target.kind == "spec" else "cards"
            columns = (await connection.exec_driver_sql(f'PRAGMA table_info("{table}")')).mappings().all()
            names = [column["name"] for column in columns]
            quoted = ['"' + name.replace('"', '""') + '"' for name in names]
            length = "+".join(f"coalesce(length(CAST({name} AS BLOB)),0)" for name in quoted)
            amount = (await connection.execute(text(f'SELECT {length} FROM "{table}" WHERE id=:id'), {"id": target.identity})).scalar_one_or_none()
            if amount is None:
                raise ValueError("context_disposition_target_missing")
            if (target.kind, target.identity) not in snapshots:
                size += amount
            if size > _MAX_BYTES:
                raise ValueError("context_disposition_limit")
            row = (await connection.execute(text(f'SELECT * FROM "{table}" WHERE id=:id'), {"id": target.identity})).mappings().one()
            require_context_target_scope(origin_board=population[decision.candidate_sha256].board_id, target_board=row["board_id"])
            snapshots[target.kind, target.identity] = _digest([(name, _cell(row[name])) for name in names])
    return snapshots


def _bindings(records):
    bindings = []
    for record in records:
        if record["event_type"] != _EVENT:
            continue
        payload = record["payload_json"]
        candidate, decision = payload["candidate"], payload["decision"]
        for target in decision["targets"]:
            bindings.append({"id": _id(payload["migration_id"], "binding", [decision["candidate_sha256"], target]),
                "board_id": record["board_id"], "event_type": _BINDING, "actor_type": "system", "actor_id": None,
                "payload_json": {"format": _FORMAT, "migration_id": payload["migration_id"],
                    "audit_id": _id(payload["migration_id"], "audit", record["board_id"]),
                    "archive_id": payload["archive_id"], "archive_sha256": payload["archive_sha256"],
                    "origin_kind": "sprint", "origin_id": candidate["origin_id"],
                    "candidate_sha256": decision["candidate_sha256"], "source_sha256": candidate["source_sha256"],
                    "target": target, "selection": payload["selection"]}})
    return bindings


def _audit(reference, records):
    return {"format": _FORMAT, "migration_id": reference.migration_id, "board_id": reference.board_id,
        "archive_id": reference.event_id, "archive_sha256": reference.sha256,
        "records": [row for row in records if row["board_id"] == reference.board_id]}


def _manifest(reference, content, path):
    return {"id": _id(reference.migration_id, "audit", reference.board_id), "board_id": reference.board_id,
        "event_type": _COMMITTED, "actor_type": "system", "actor_id": None,
        "payload_json": {"format": _FORMAT, "migration_id": reference.migration_id,
            "archive_id": reference.event_id, "archive_sha256": reference.sha256,
            "storage_path": path, "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}}


async def _evidence(storage, previous, references):
    manifests = {row["board_id"]: row for row in previous if row["event_type"] == _COMMITTED}
    if len(manifests) != len(references) or sum(row["event_type"] == _COMMITTED for row in previous) != len(manifests):
        raise ValueError("context_disposition_evidence_mismatch")
    records, expected_manifests, size = [], [], 0
    for reference in references:
        manifest = manifests[reference.board_id]
        metadata = manifest["payload_json"]
        if (type(metadata.get("size")) is not int or not 0 < metadata["size"] <= _MAX_BYTES
                or type(metadata.get("storage_path")) is not str or not metadata["storage_path"]):
            raise ValueError("context_disposition_evidence_mismatch")
        size += metadata["size"]
        if size > _MAX_BYTES:
            raise ValueError("context_disposition_limit")
        if (await storage.stat(metadata["storage_path"])).size != metadata["size"]:
            raise ValueError("context_disposition_evidence_mismatch")
        content = bytearray()
        async for chunk in storage.open_stream(metadata["storage_path"]):
            content.extend(chunk)
            if len(content) > metadata["size"]:
                raise ValueError("context_disposition_evidence_mismatch")
        expected_manifest = _manifest(reference, content, metadata["storage_path"])
        if manifest != expected_manifest:
            raise ValueError("context_disposition_evidence_mismatch")
        payload = json.loads(content)
        if type(payload) is not dict or type(payload.get("records")) is not list or len(payload["records"]) > 100_000:
            raise ValueError("context_disposition_evidence_mismatch")
        if payload != _audit(reference, payload["records"]):
            raise ValueError("context_disposition_evidence_mismatch")
        records.extend(payload["records"])
        if len(records) > 100_000:
            raise ValueError("context_disposition_limit")
        expected_manifests.append(expected_manifest)
    return records, expected_manifests


async def require_context_dispositions(connection, storage, references, candidates=None, *, expected_receipt=None,
    expected_plan=None, check_targets=True):
    """Verify committed evidence under the Card step's existing transaction."""
    references = tuple(sorted(references, key=lambda reference: reference.board_id))
    documents = await _documents(connection, storage, references)
    migration = references[0].migration_id
    previous = await _journal(connection, migration)
    if not previous:
        if expected_receipt is not None:
            raise ValueError("context_disposition_replay_mismatch")
        raise SprintContextDispositionRequired(candidates or ())
    records, manifests = await _evidence(storage, previous, references)
    manifest = next((row for row in records if row["event_type"] == _COMPLETE), None)
    if manifest is None:
        raise ValueError("context_disposition_evidence_mismatch")
    plan = ContextDispositionPlan.model_validate_json(_encode({"migration_id": migration,
        "decision_reference": manifest["payload_json"]["decision_reference"],
        "decisions": [row["payload_json"]["decision"] for row in records if row["event_type"] == _EVENT]}))
    if expected_plan is not None:
        if (expected_plan.migration_id != plan.migration_id or expected_plan.decision_reference != plan.decision_reference
                or set(expected_plan.decisions) != set(plan.decisions)):
            raise ValueError("context_disposition_replay_mismatch")
    if candidates is None:
        candidates = tuple(SprintContextCandidate(**{**row["payload_json"]["candidate"],
            "key": tuple(tuple(pair) for pair in row["payload_json"]["candidate"]["key"]),
            "path": tuple(row["payload_json"]["candidate"]["path"])}) for row in records if row["event_type"] == _EVENT)
    receipt, expected = _records(references, documents, candidates, plan)
    if (sorted(records, key=lambda row: row["id"]) != sorted(expected, key=lambda row: row["id"])
            or sorted(previous, key=lambda row: row["id"]) != sorted(manifests + _bindings(expected), key=lambda row: row["id"])
            or expected_receipt is not None and expected_receipt != receipt):
        raise ValueError("context_disposition_evidence_mismatch")
    if check_targets:
        await _targets(connection, candidates, plan)
    return receipt


async def install_context_dispositions(engine, storage, references, *, plan: ContextDispositionPlan, expected_receipt=None):
    if engine.dialect.name != "sqlite" or not isinstance(plan, ContextDispositionPlan):
        raise ValueError("context_disposition_input_invalid")
    references = tuple(sorted(references, key=lambda reference: reference.board_id))
    if any(reference.migration_id != plan.migration_id for reference in references):
        raise ValueError("context_disposition_archive_scope_invalid")
    async with engine.connect() as connection:
        created_paths, commit_started = [], False
        try:
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            documents = await _documents(connection, storage, references)
            previous = await _journal(connection, plan.migration_id)
            if previous:
                receipt = await require_context_dispositions(connection, storage, references,
                    expected_plan=plan, expected_receipt=expected_receipt, check_targets=False)
                await connection.commit()
                return receipt
            if expected_receipt is not None:
                raise ValueError("context_disposition_replay_mismatch")
            inventory = await connection.run_sync(inspect_sprint_pretransform)
            inventory.relational.work.require_classified_work()
            receipt, expected = _records(references, documents, inventory.context_candidates, plan)
            completed_card = await connection.scalar(select(_TABLE.c.id).where(
                _TABLE.c.event_type == "migration.card_validation_preserved",
                _TABLE.c.payload_json["migration_id"].as_string() == plan.migration_id).limit(1))
            if completed_card is not None:
                raise ValueError("context_disposition_stage_order_invalid")
            await _require_original_archive(connection, references, plan.migration_id)
            target_snapshots = await _targets(connection, inventory.context_candidates, plan)
            manifests, size = [], 0
            for reference in references:
                content = _encode(_audit(reference, expected))
                size += len(content)
                if size > _MAX_BYTES:
                    raise ValueError("context_disposition_limit")
                path = await storage.save(reference.board_id, f"context-history-{_id(plan.migration_id, 'audit', reference.board_id)}.json", content)
                created_paths.append(path)
                manifests.append(_manifest(reference, content, path))
            journal = manifests + _bindings(expected)
            if len(journal) > 100_000 or len(_encode(journal)) > _MAX_BYTES:
                raise ValueError("context_disposition_limit")
            await connection.execute(insert(_TABLE), [{**row, "occurred_at": datetime.now(timezone.utc)} for row in journal])
            # Triggers on the journal must not change the classified sources.
            after = await connection.run_sync(inspect_sprint_pretransform)
            if (after.context_candidates != inventory.context_candidates or after.relational.counts != inventory.relational.counts
                    or after.relational.work.items != inventory.relational.work.items
                    or after.relational.historical_references.references != inventory.relational.historical_references.references
                    or after.relational.embedded_references.references != inventory.relational.embedded_references.references
                    or await _targets(connection, inventory.context_candidates, plan) != target_snapshots):
                raise ValueError("context_disposition_source_changed")
            await _require_original_archive(connection, references, plan.migration_id)
            if await require_context_dispositions(connection, storage, references, inventory.context_candidates) != receipt:
                raise ValueError("context_disposition_evidence_mismatch")
            commit_started = True
            await connection.commit()
            return receipt
        except BaseException:
            await connection.rollback()
            if not commit_started:
                for path in created_paths:
                    try:
                        await storage.delete(path)
                    except Exception:
                        pass
            raise
