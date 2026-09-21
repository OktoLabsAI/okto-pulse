"""Internal F2B cutover step, only after archived history and grants exist.

Deprecated Card compatibility preserves necessary migrated overrides; it is not
an executor-editable policy hierarchy. Do not expire it by time. This operation
is not registered in bootstrap: the full F2/F3 coordinator must also reconcile
substantive context, work, graph references and schema before enabling runtime.
"""

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import re
import uuid

from sqlalchemy import Boolean, Column, Integer, LargeBinary, MetaData, String, Table, cast, func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from okto_pulse.core import StorageProvider
from okto_pulse.core.ports.card_validation_migration import (
    plan_card_validation_migration, verify_card_validation_migration,
)
from okto_pulse.community.adapters.context_disposition_retirement import (
    ContextDispositionReceipt, require_context_dispositions, verify_context_disposition_snapshot,
)
from okto_pulse.community.adapters.retirement_data_journal import require_retirement_stage, record_retirement_stage
from okto_pulse.community.adapters.historical_archive_grant_installation import _install_historical_archive_grants
from okto_pulse.community.adapters.sprint_retirement_archive import (
    HistoricalArchiveReference, _attach_access, _capture, _cell, _encode,
)
from okto_pulse.community.adapters.sqlalchemy_models import Board, DomainEventRow, Spec
from okto_pulse.community.adapters.sprint_retirement_preflight import inspect_sprint_pretransform

_EVENT = "migration.card_validation_preserved"
_FORMAT = "card-validation-retirement/v1"
# v1 remains readable for steps completed with no substantive context. v2 binds
# the explicit disposition receipt; never retrofit or silently downgrade it.
_CONTEXT_FORMAT = "card-validation-retirement/v2"
_NAMESPACE = uuid.UUID("2ae6c2bd-a989-5dd6-ad61-59e01c4eae70")
_MAX_BYTES = 64 * 1024 * 1024
_ATTRIBUTES = ("require_task_validation", "validation_min_confidence", "validation_min_completeness", "validation_max_drift")
_EVENTS = DomainEventRow.__table__


def _digest(value):
    return hashlib.sha256(_encode(value)).hexdigest()


def _identity(migration_id, board_id):
    return str(uuid.uuid5(_NAMESPACE, _encode([migration_id, board_id]).decode("utf-8")))


def _reference(reference):
    return json.loads(_encode(asdict(reference)))


@dataclass(frozen=True, slots=True)
class CardValidationRetirementReceipt:
    migration_id: str
    evidence_sha256: str
    card_count: int
    override_count: int

    def __post_init__(self):
        if (type(self.migration_id) is not str or not 1 <= len(self.migration_id) <= 128
                or type(self.evidence_sha256) is not str or re.fullmatch(r"[0-9a-f]{64}", self.evidence_sha256) is None
                or type(self.card_count) is not int or not 0 <= self.card_count <= 100_000
                or type(self.override_count) is not int or not 0 <= self.override_count <= self.card_count):
            raise ValueError("card_validation_retirement_receipt_invalid")


def _receipt(migration_id, payloads):
    return CardValidationRetirementReceipt(migration_id, _digest(payloads),
        sum(len(payload["cards"]) for payload in payloads),
        sum(card["policy"] is not None for payload in payloads for card in payload["cards"]))


async def _read_events(connection, migration_id):
    selection = (_EVENTS.c.event_type == _EVENT,
        _EVENTS.c.payload_json["migration_id"].as_string() == migration_id)
    count, size = (await connection.execute(select(func.count(), func.coalesce(func.sum(
        func.length(cast(_EVENTS.c.payload_json, LargeBinary))), 0)).where(*selection))).one()
    if count > 100_000 or size > _MAX_BYTES:
        raise ValueError("card_validation_retirement_limit")
    return (await connection.execute(select(_EVENTS).where(*selection).order_by(_EVENTS.c.board_id))).mappings().all()


async def _verify_events(events, references, migration_id, storage, *, connection=None, context_receipt=None, verify_live=False):
    if len(events) != len(references):
        raise ValueError("card_validation_retirement_evidence_mismatch")
    payloads, seen, total_size = [], set(), 0
    if verify_live:
        if connection is None:
            raise ValueError("card_validation_retirement_live_connection_required")
        live_cards = {row['id']: row for row in await _load_cards(connection, linked_only=False)}
        live_layers = await _load_policy_layers(connection, include_sprints=False)
    formats, context_receipts = set(), []
    for event, reference in zip(events, references, strict=True):
        manifest = event["payload_json"]
        version = manifest.get("format") if type(manifest) is dict else None
        extra = {"context_receipt"} if version == _CONTEXT_FORMAT else set()
        if (type(manifest) is not dict or set(manifest) != {"format", "migration_id", "archive", "storage_path",
                "sha256", "size", "card_count", "override_count"} | extra
                or version not in {_FORMAT, _CONTEXT_FORMAT} or manifest["migration_id"] != migration_id
                or manifest["archive"] != _reference(reference)
                or type(manifest["storage_path"]) is not str or not manifest["storage_path"]
                or type(manifest["size"]) is not int or not 0 < manifest["size"] <= _MAX_BYTES
                or type(manifest["sha256"]) is not str or re.fullmatch(r"[0-9a-f]{64}", manifest["sha256"]) is None
                or any(type(manifest[key]) is not int or not 0 <= manifest[key] <= 100_000
                    for key in ("card_count", "override_count"))):
            raise ValueError("card_validation_retirement_evidence_mismatch")
        total_size += manifest["size"]
        if total_size > _MAX_BYTES:
            raise ValueError("card_validation_retirement_limit")
        if (await storage.stat(manifest["storage_path"])).size != manifest["size"]:
            raise ValueError("card_validation_retirement_evidence_mismatch")
        content = bytearray()
        async for chunk in storage.open_stream(manifest["storage_path"]):
            content.extend(chunk)
            if len(content) > manifest["size"]:
                raise ValueError("card_validation_retirement_evidence_mismatch")
        if len(content) != manifest["size"] or hashlib.sha256(content).hexdigest() != manifest["sha256"]:
            raise ValueError("card_validation_retirement_evidence_mismatch")
        payload = json.loads(content)
        if (event["id"] != _identity(migration_id, reference.board_id) or event["board_id"] != reference.board_id
                or event["event_type"] != _EVENT or event["actor_type"] != "system" or event["actor_id"] is not None
                or type(payload) is not dict or set(payload) != {"format", "migration_id", "archive", "cards"} | extra
                or payload["format"] != version or payload["migration_id"] != migration_id
                or payload["archive"] != _reference(reference) or type(payload["cards"]) is not list):
            raise ValueError("card_validation_retirement_evidence_mismatch")
        formats.add(version)
        if version == _CONTEXT_FORMAT:
            bound = payload["context_receipt"]
            if (type(bound) is not dict or set(bound) != {"migration_id", "evidence_sha256", "candidate_count", "binding_count"}
                    or bound != manifest["context_receipt"]):
                raise ValueError("card_validation_retirement_context_mismatch")
            bound = ContextDispositionReceipt(**bound)
            if bound.migration_id != migration_id:
                raise ValueError("card_validation_retirement_context_mismatch")
            context_receipts.append(bound)
        identities = []
        for entry in payload["cards"]:
            if (type(entry) is not dict or set(entry) != {"facts", "before", "after", "policy", "row_before", "row_after"}
                    or type(entry["facts"]) is not dict
                    or set(entry["facts"]) != {"card", "spec", "sprint", "board_settings"}
                    or any(type(entry[key]) is not str or re.fullmatch(r"[0-9a-f]{64}", entry[key]) is None
                        for key in ("row_before", "row_after"))):
                raise ValueError("card_validation_retirement_evidence_mismatch")
            facts = entry["facts"]
            plan = plan_card_validation_migration(**facts, migration_id=migration_id)
            policy = plan.policy.model_dump(mode="json", exclude_none=True) if plan.policy else None
            if (entry["before"] != plan.before or entry["after"] != plan.after or entry["policy"] != policy
                    or facts["card"]["board_id"] != reference.board_id or facts["card"]["id"] in seen):
                raise ValueError("card_validation_retirement_evidence_mismatch")
            seen.add(facts["card"]["id"])
            identities.append(facts["card"]["id"])
            if verify_live:
                live = live_cards.get(facts['card']['id'])
                if live is None or any(live[key] != facts['card'][key] for key in ('id', 'board_id', 'spec_id')):
                    raise ValueError('card_validation_retirement_live_identity_changed')
                live_policy = json.loads(live['migrated_validation_policy']) if live['migrated_validation_policy'] is not None else None
                if live_policy != entry['policy']:
                    raise ValueError('card_validation_retirement_live_policy_changed')
                verify_card_validation_migration(card={**live, 'sprint_id': None, 'migrated_validation_policy': live_policy},
                    spec=live_layers['specs'].get(live['spec_id']),
                    board_settings=live_layers['boards'][live['board_id']]['settings'] or {}, expected=entry['after'])
        if identities != sorted(identities):
            raise ValueError("card_validation_retirement_evidence_mismatch")
        if (manifest["card_count"] != len(payload["cards"])
                or manifest["override_count"] != sum(entry["policy"] is not None for entry in payload["cards"])):
            raise ValueError("card_validation_retirement_evidence_mismatch")
        payloads.append(payload)
    if len(formats) > 1 or len(set(context_receipts)) > 1:
        raise ValueError("card_validation_retirement_context_mismatch")
    bound = context_receipts[0] if context_receipts else None
    if context_receipt is not None and bound != context_receipt:
        raise ValueError("card_validation_retirement_context_mismatch")
    if bound is not None:
        if connection is None:
            raise ValueError("card_validation_retirement_context_snapshot_required")
        # Replay uses the original archive and exact journal, never live sources
        # or targets which may have legitimately changed after this step.
        await require_context_dispositions(connection, storage, references,
            expected_receipt=bound, check_targets=False)
    return _receipt(migration_id, payloads)


async def _load_cards(connection, *, linked_only=True):
    # Read raw SQL cells so status, timestamps, JSON formatting, history, assignee
    # and every unrelated column can be compared without ORM normalization.
    columns = (await connection.exec_driver_sql('PRAGMA table_info("cards")')).mappings().all()
    names = [column["name"] for column in columns]
    quoted = ['"' + name.replace('"', '""') + '"' for name in names]
    size = "+".join(f"coalesce(length(CAST({name} AS BLOB)),0)" for name in quoted)
    scope = " WHERE sprint_id IS NOT NULL" if linked_only else ""
    count, total = (await connection.execute(text(
        f"SELECT count(*),coalesce(sum({size}),0) FROM cards{scope}"))).one()
    if count > 100_000 or total > _MAX_BYTES:
        raise ValueError("card_validation_retirement_limit")
    return [dict(row) for row in (await connection.execute(text(
        f"SELECT * FROM cards{scope} ORDER BY id"))).mappings()]


async def _load_policy_layers(connection, *, include_sprints=True):
    # Historical SQL projection only. Never register a retired table or mapper
    # in the operational metadata used by fresh installation.
    legacy_sprint = Table("sprints", MetaData(), Column("id", String), Column("board_id", String),
        Column("require_task_validation", Boolean),
        *(Column(name, Integer) for name in _ATTRIBUTES if name != "require_task_validation"))
    layers, count_total, size_total = {}, 0, 0
    selected = ((Board.__table__, ("settings",)), (Spec.__table__, ("board_id", *_ATTRIBUTES)))
    if include_sprints:
        selected += ((legacy_sprint, ("board_id", *_ATTRIBUTES)),)
    for table, fields in selected:
        count, size = (await connection.execute(select(func.count(), func.coalesce(func.sum(sum(
            func.coalesce(func.length(cast(table.c[name], LargeBinary)), 0) for name in ("id", *fields))), 0)))).one()
        count_total += count
        size_total += size
        if count_total > 100_000 or size_total > _MAX_BYTES:
            raise ValueError("card_validation_retirement_limit")
        layers[table.name] = {row["id"]: dict(row) for row in (await connection.execute(select(
            table.c.id, *(table.c[name] for name in fields)))).mappings()}
    return layers


async def _read_raw_card(connection, identity, columns):
    quoted = ['"' + name.replace('"', '""') + '"' for name in columns]
    size = "+".join(f"coalesce(length(CAST({name} AS BLOB)),0)" for name in quoted)
    length = (await connection.execute(text(f"SELECT {size} FROM cards WHERE id=:id"), {"id": identity})).scalar_one_or_none()
    if length is None:
        raise ValueError("card_validation_retirement_write_mismatch")
    if length > _MAX_BYTES:
        raise ValueError("card_validation_retirement_limit")
    return dict((await connection.execute(text("SELECT * FROM cards WHERE id=:id"), {"id": identity})).mappings().one())


async def _require_context_fence(connection, storage, references, before, receipt, targets, expected_rows):
    """Allow only exact Card outputs, including an added NULL compatibility cell."""
    _, current_targets = await verify_context_disposition_snapshot(connection, storage, references,
        expected_receipt=receipt)
    named_hashes = {identity: _digest([(name, _cell(value)) for name, value in row.items()])
        for identity, row in expected_rows.items()}
    expected_targets = {key: named_hashes[key[1]] if key[0] == "card" and key[1] in named_hashes else value
        for key, value in targets.items()}
    if current_targets != expected_targets:
        raise ValueError("card_validation_retirement_context_target_changed")
    after = await connection.run_sync(inspect_sprint_pretransform)
    after.require_resolved_mechanics()
    policies = {identity: json.loads(row["migrated_validation_policy"])
        for identity, row in expected_rows.items() if row["migrated_validation_policy"] is not None}

    def generated_reference(table, key, path, origin):
        identity = dict(key).get("id")
        return (table == "cards" and key == (("id", identity),)
            and path == ("migrated_validation_policy", "source_sprint_id")
            and identity in policies and origin == policies[identity]["source_sprint_id"])

    original = {(item.table, item.key, item.path): item for item in before.context_candidates}
    normalized = []
    for candidate in after.context_candidates:
        identity = dict(candidate.key).get("id")
        # New provenance is mechanical only when the complete Card row equals
        # the migration's exact output. Existing embedded context still needs
        # its original disposition; no path/name-wide exception is introduced.
        if candidate.table == "cards" and candidate.source_sha256 == named_hashes.get(identity):
            if generated_reference(candidate.table, candidate.key, candidate.path, candidate.origin_id):
                continue
            prior = original.get((candidate.table, candidate.key, candidate.path))
            if prior is not None:
                candidate = replace(candidate, source_sha256=prior.source_sha256)
        normalized.append(candidate)
    expected_counts = {**dict(before.relational.counts), "linked_cards": 0}
    embedded = tuple(reference for reference in after.relational.embedded_references.references
        if not generated_reference(reference.table, reference.key,
            (reference.column, *reference.path), reference.sprint_id))
    if (sorted(_encode(asdict(item)) for item in normalized)
            != sorted(_encode(asdict(item)) for item in before.context_candidates)
            or dict(after.relational.counts) != expected_counts
            or after.relational.work.items != before.relational.work.items
            or after.relational.historical_references.references != before.relational.historical_references.references
            or embedded != before.relational.embedded_references.references):
        raise ValueError("card_validation_retirement_context_source_changed")


async def materialize_archived_card_policies(
    engine: AsyncEngine, storage: StorageProvider, references: tuple[HistoricalArchiveReference, ...], *,
    migration_id: str, expected_receipt: CardValidationRetirementReceipt | None = None,
    context_receipt: ContextDispositionReceipt | None = None, checkpoint_run=None,
) -> CardValidationRetirementReceipt:
    """Atomically preserve policy and detach Cards; never rebase on a replay.

    Retain the receipt in the enclosing migration ledger to detect journal loss.
    This step does not remove Sprint tables, process work or create approvals.
    """
    if engine.dialect.name != "sqlite":
        raise ValueError("card_validation_retirement_backend_unsupported")
    from okto_pulse.community.adapters.relational_schema_steps import _ensure_card_validation_compatibility
    _receipt(migration_id, [])  # Validate identity even for an empty population.
    if context_receipt is not None and (not isinstance(context_receipt, ContextDispositionReceipt)
            or context_receipt.migration_id != migration_id):
        raise ValueError("card_validation_retirement_context_mismatch")
    references = tuple(sorted(references, key=lambda ref: ref.board_id))
    if (len({ref.board_id for ref in references}) != len(references)
            or any(ref.migration_id != migration_id for ref in references)
            or sum(ref.size for ref in references) > _MAX_BYTES):
        raise ValueError("card_validation_retirement_archive_scope_invalid")
    async with engine.connect() as connection:
        created_paths, commit_started = [], False
        try:
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            await require_retirement_stage(connection, checkpoint_run, "cards", references, dependency=context_receipt)
            for reference in references:
                await _install_historical_archive_grants(connection, storage, reference, require_existing=True)
            previous = await _read_events(connection, migration_id)
            add_column = await _ensure_card_validation_compatibility(connection, create=False) == "missing"
            if previous:
                if add_column:
                    raise ValueError("card_validation_retirement_storage_missing")
                receipt = await _verify_events(previous, references, migration_id, storage,
                    connection=connection, context_receipt=context_receipt)
                if expected_receipt is not None and expected_receipt != receipt:
                    raise ValueError("card_validation_retirement_replay_mismatch")
                await record_retirement_stage(connection, checkpoint_run, "cards", receipt, replay=True)
                await connection.commit()
                return receipt
            if expected_receipt is not None and references:
                raise ValueError("card_validation_retirement_replay_mismatch")
            preflight = await connection.run_sync(inspect_sprint_pretransform)
            preflight.require_resolved_mechanics()
            target_snapshots = {}
            if context_receipt is None:
                preflight.require_resolved_pretransform()
            else:
                _, target_snapshots = await verify_context_disposition_snapshot(connection, storage, references,
                    preflight.context_candidates, expected_receipt=context_receipt)
            # Exact recapture makes stale archive content or missing/extra origins
            # a closed failure. The archive must precede permission cleanup too.
            captures = await connection.run_sync(lambda sync: _capture(sync,
                migration_id=migration_id, max_rows=100_000, max_bytes=_MAX_BYTES))
            captures = await _attach_access(connection, captures, max_rows=100_000, max_bytes=_MAX_BYTES)
            if [(identity, board, hashlib.sha256(content).hexdigest(), len(content), counts)
                    for identity, board, content, counts in captures] != [
                    (ref.event_id, ref.board_id, ref.sha256, ref.size, ref.counts) for ref in references]:
                raise ValueError("card_validation_retirement_archive_changed")
            # Capture original raw cells before additive DDL. Every unlinked
            # Card also gains NULL, changing its context hash; accept only that
            # exact output and verify it again after all triggers have fired.
            source_cards = await _load_cards(connection, linked_only=not add_column)
            cards = [row for row in source_cards if row["sprint_id"] is not None]
            expected_rows = {row["id"]: {**row, "migrated_validation_policy": None}
                for row in source_cards if row["sprint_id"] is None} if add_column else {}
            if add_column:
                await _ensure_card_validation_compatibility(connection)
            population = (await connection.execute(text("SELECT count(*) FROM cards"))).scalar_one()
            version = _CONTEXT_FORMAT if context_receipt is not None else _FORMAT
            bound_context = {"context_receipt": asdict(context_receipt)} if context_receipt is not None else {}
            payloads = {ref.board_id: {"format": version, "migration_id": migration_id,
                "archive": _reference(ref), "cards": [], **bound_context} for ref in references}
            audit_size = len(json.dumps(list(payloads.values()), allow_nan=False).encode("utf-8"))
            layers = await _load_policy_layers(connection)
            for row in cards:
                facts = {"card": {name: row[name] for name in ("id", "board_id", "spec_id", "sprint_id")},
                    "spec": layers["specs"].get(row["spec_id"]), "sprint": layers["sprints"][row["sprint_id"]],
                    "board_settings": layers["boards"][row["board_id"]]["settings"] or {}}
                facts["card"]["migrated_validation_policy"] = json.loads(row["migrated_validation_policy"]) if row.get("migrated_validation_policy") is not None else None
                plan = plan_card_validation_migration(**facts, migration_id=migration_id)
                policy = plan.policy.model_dump(mode="json", exclude_none=True) if plan.policy else None
                serialized = json.dumps(policy, ensure_ascii=False, allow_nan=False) if policy else None
                expected = {**row, "sprint_id": None, "migrated_validation_policy": serialized}
                expected_rows[row["id"]] = expected
                # Raw SQL deliberately avoids ORM onupdate timestamps and events.
                await connection.execute(text("UPDATE cards SET sprint_id=NULL,migrated_validation_policy=:policy WHERE id=:id"),
                    {"policy": serialized, "id": row["id"]})
                persisted = await _read_raw_card(connection, row["id"], expected)
                if _encode([_cell(value) for value in persisted.values()]) != _encode([_cell(value) for value in expected.values()]):
                    raise ValueError("card_validation_retirement_write_mismatch")
                verify_card_validation_migration(card={**facts["card"], "sprint_id": persisted["sprint_id"],
                    "migrated_validation_policy": json.loads(persisted["migrated_validation_policy"]) if persisted["migrated_validation_policy"] is not None else None},
                    spec=facts["spec"], board_settings=facts["board_settings"], expected=plan.after)
                entry = {"facts": facts, "before": plan.before, "after": plan.after,
                    "policy": policy, "row_before": _digest([_cell(value) for value in row.values()]),
                    "row_after": _digest([_cell(value) for value in persisted.values()])}
                # Bound repeated layer facts incrementally, before constructing
                # an aggregate serializer string from a large Card population.
                audit_size += len(json.dumps(entry, allow_nan=False).encode("utf-8")) + 2
                if audit_size > _MAX_BYTES:
                    raise ValueError("card_validation_retirement_limit")
                payloads[row["board_id"]]["cards"].append(entry)
            ordered = list(payloads.values())
            if len(json.dumps(ordered, allow_nan=False).encode("utf-8")) > _MAX_BYTES:
                raise ValueError("card_validation_retirement_limit")
            receipt = _receipt(migration_id, ordered)
            if expected_receipt is not None and expected_receipt != receipt:
                raise ValueError("card_validation_retirement_replay_mismatch")
            for board, payload in payloads.items():
                content = _encode(payload)
                path = await storage.save(board, f"card-policy-history-{_identity(migration_id, board)}.json", content)
                created_paths.append(path)
                manifest = {"format": version, "migration_id": migration_id, "archive": payload["archive"], **bound_context,
                    "storage_path": path, "sha256": hashlib.sha256(content).hexdigest(), "size": len(content),
                    "card_count": len(payload["cards"]),
                    "override_count": sum(entry["policy"] is not None for entry in payload["cards"])}
                await connection.execute(insert(_EVENTS).values(id=_identity(migration_id, board), board_id=board,
                    event_type=_EVENT, actor_type="system", actor_id=None, occurred_at=datetime.now(timezone.utc), payload_json=manifest))
            await record_retirement_stage(connection, checkpoint_run, "cards", receipt, replay=False)
            if await _verify_events(await _read_events(connection, migration_id), references, migration_id, storage,
                    connection=connection, context_receipt=context_receipt) != receipt:
                raise ValueError("card_validation_retirement_evidence_mismatch")
            if _digest(await _load_policy_layers(connection)) != _digest(layers):
                raise ValueError("card_validation_retirement_policy_changed")
            # A later update's trigger must not alter a previously verified Card.
            for identity, expected in expected_rows.items():
                stored = await _read_raw_card(connection, identity, expected)
                if _encode([_cell(value) for value in stored.values()]) != _encode([_cell(value) for value in expected.values()]):
                    raise ValueError("card_validation_retirement_write_mismatch")
            if ((await connection.execute(text("SELECT count(*) FROM cards"))).scalar_one() != population
                    or (await connection.execute(text("SELECT 1 FROM cards WHERE sprint_id IS NOT NULL LIMIT 1"))).first()):
                raise ValueError("card_validation_retirement_population_changed")
            if context_receipt is not None:
                await _require_context_fence(connection, storage, references, preflight,
                    context_receipt, target_snapshots, expected_rows)
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
                        pass  # Preserve the original failure; no committed reference exposes this blob.
            raise
