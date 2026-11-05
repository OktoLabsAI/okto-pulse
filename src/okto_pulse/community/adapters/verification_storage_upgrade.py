"""Explicit source-preserving format406-to407 conversion with current-only seeds."""

from collections import Counter
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from okto_pulse.core.domain.verification_currentness import (
    VerificationContext, result_from_authenticated_legacy_evidence,
)
from okto_pulse.core.domain.verification_report import parse_verification_report, require_verification_report_context
from okto_pulse.core.services.test_scenario_lifecycle import compute_test_scenario_semantic_sha256

from .current_relational_schema import (
    SCHEMA_VERSION, StorageFormatError, _read_without_side_effects, current_schema_contract, inspect_current_schema,
)
from .historical_relational_schema import FORMAT406_SHA256, historical_schema_contract, inspect_historical_schema
from .sqlite_verification_currentness import install_verification_currentness_functions, scenario_facts
from .storage_upgrade_v043 import StorageUpgradeError, _source_state, _table_fingerprint
from .test_evidence import CommunityEvidenceLedger, CommunityTestEvidenceWriteVerifier

NEW_TABLES = frozenset({"verification_scenario_states", "verification_reservations",
                       "verification_acceptances", "verification_basis_heads"})


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _insert(connection, table, values):
    # Names are internal constants, never caller-controlled SQL identifiers.
    columns = ",".join(values)
    connection.execute(f"INSERT INTO {table} ({columns}) VALUES ({','.join('?' for _ in values)})", tuple(values.values()))


def _seed_current_facts(connection, *, ledger, source_snapshot):
    verifier = CommunityTestEvidenceWriteVerifier(ledger=ledger)
    counts = Counter()
    query = "SELECT id,board_id,edition,test_scenarios,acceptance_criteria FROM specs ORDER BY id"
    for spec_id, board_id, edition, raw_scenarios, raw_criteria in connection.execute(query):
        scenarios = json.loads(raw_scenarios or "null") or []
        criteria = json.loads(raw_criteria or "null") or []
        facts = json.loads(scenario_facts(board_id, spec_id, raw_scenarios, raw_criteria, "[]"))
        by_id = {item["id"]: item for item in scenarios}
        for fact in facts:
            scope = {"board_id": board_id, "spec_id": spec_id, "spec_edition": edition, "scenario_id": fact["id"]}
            _insert(connection, "verification_scenario_states", {**scope, "generation": 1,
                "last_ordinal": 0, "summary_ordinal": 0, "revision": 1, "scenario_sha256": fact["digest"],
                "verification_method": fact["method"], "deleted": 0})
            counts["scenarios"] += 1
            scenario = by_id[fact["id"]]
            status, evidence = scenario.get("status"), scenario.get("evidence")
            if status not in {"passed", "failed"}:
                counts["no_current_terminal_fact"] += 1
                continue
            try:
                if not isinstance(evidence, dict) or evidence.get("verification_context") is not None:
                    raise ValueError("verification_legacy_seed_invalid")
                digest = compute_test_scenario_semantic_sha256(board_id=board_id, spec_id=spec_id,
                    scenario=scenario, acceptance_criteria=criteria)
                actor = ledger.authenticated_actor_id(board_id=board_id, spec_id=spec_id, scenario_id=fact["id"],
                    scenario_sha256=digest, status=status, evidence=evidence)
                verdict = verifier.verify(board_id=board_id, spec_id=spec_id, scenario_id=fact["id"],
                    scenario_sha256=digest, status=status, actor_id=actor, evidence=evidence)
                if not verdict.verified:
                    raise ValueError("verification_legacy_seed_unqualified")
                if evidence.get("evidence_class") == "verification_report":
                    require_verification_report_context(parse_verification_report(evidence["verification_report"]),
                        method=fact["method"], status=status, criterion_ids=tuple(scenario.get("linked_criteria") or ()))
                identity = hashlib.sha256(_json([source_snapshot, scope]).encode()).hexdigest()
                context = VerificationContext(**scope, reservation_id="verification_migration_" + identity,
                    scenario_sha256=digest.removeprefix("sha256:"), verification_method=fact["method"],
                    generation=1, ordinal=1)
                result = result_from_authenticated_legacy_evidence(evidence, context=context, actor_id=actor)
                if result.outcome != status:
                    raise ValueError("verification_legacy_seed_invalid")
            except (TypeError, ValueError, KeyError, OSError):
                # Retain original rows and receipts exactly. Missing authority
                # is explicit unqualified proof, never historical pass backfill.
                counts["unqualified_current_fact"] += 1
                continue
            acceptance_id = "verification_migrated_acceptance_" + identity
            _insert(connection, "verification_reservations", {**scope, "id": context.reservation_id,
                "generation": 1, "ordinal": 1, "context": _json(context.model_dump(mode="json")),
                "actor_id": actor, "actor_kind": "unknown"})
            _insert(connection, "verification_acceptances", {**scope, "id": acceptance_id,
                "reservation_id": context.reservation_id, "generation": 1, "ordinal": 1,
                "basis_digest": result.basis.digest, "receipt_id": result.receipt_id,
                "result": _json(result.model_dump(mode="json")), "evidence": _json(evidence),
                "evidence_status": status, "origin": "legacy_seed", "migration_source_sha256": source_snapshot})
            _insert(connection, "verification_basis_heads", {**scope, "generation": 1, "ordinal": 1,
                "basis_digest": result.basis.digest, "accepted_result_id": acceptance_id})
            connection.execute("UPDATE verification_scenario_states SET last_ordinal=1,summary_ordinal=1,revision=2 "
                "WHERE board_id=? AND spec_id=? AND spec_edition=? AND scenario_id=?", tuple(scope.values()))
            counts["seeded_" + status] += 1
    return dict(counts)


def _convert_copy(connection, *, target, ledger, source_snapshot):
    before = {(row[0], row[1]): row for row in historical_schema_contract().objects}
    after = {(row[0], row[1]): row for row in target.objects}
    if not before.keys() <= after.keys():
        raise StorageUpgradeError("unsupported_schema_delta")
    changed = [after[key] for key, row in before.items() if after[key] != row]
    additions = [row for key, row in after.items() if key not in before]
    if (any(row[0] != "trigger" or not row[1].startswith("trg_readiness_") for row in changed)
        or {row[1] for row in additions if row[0] == "table"} != NEW_TABLES
        or any(row[0] not in {"table", "index", "trigger"} or
            (row[2] not in NEW_TABLES and not (row[0] == "trigger" and row[1].startswith("trg_verification_spec_")))
            for row in additions)):
        raise StorageUpgradeError("unsupported_schema_delta")
    tables = {row[1] for row in before.values() if row[0] == "table"}
    preserved = {table: _table_fingerprint(connection, table) for table in sorted(tables)}
    connection.execute("BEGIN IMMEDIATE")
    try:
        # Seed before installing new mutation hooks: historical business and
        # derived rows remain byte-for-byte identical, including frozen choices.
        for row in sorted(additions, key=lambda row: (row[0] != "table", row[1])):
            if row[0] != "trigger":
                connection.execute(row[3])
        seeds = _seed_current_facts(connection, ledger=ledger, source_snapshot=source_snapshot)
        for row in changed:
            connection.execute('DROP TRIGGER "' + row[1].replace('"', '""') + '"')
        for row in [*changed, *(row for row in additions if row[0] == "trigger")]:
            connection.execute(row[3])
        connection.execute("PRAGMA main.user_version=407")
        if {table: _table_fingerprint(connection, table) for table in sorted(tables)} != preserved:
            raise StorageUpgradeError("source_rows_changed")
        inspect_current_schema(connection.cursor(), target)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return preserved, seeds, len(additions), len(changed)


def upgrade_verification_copy(source: Path, output: Path, *, evidence_root: Path):
    source, output = Path(source).expanduser().resolve(), Path(output).expanduser().resolve()
    if not source.is_file():
        raise StorageUpgradeError("source_file_required")
    if output.exists() or not output.parent.is_dir() or source == output:
        raise StorageUpgradeError("unused_output_file_required")
    if SCHEMA_VERSION != 407:
        raise StorageUpgradeError("target_version")
    target = current_schema_contract()
    descriptor, name = tempfile.mkstemp(prefix=".pulse-verification-upgrade-", suffix=".db", dir=output.parent)
    os.close(descriptor)
    pending = Path(name)
    try:
        source_state = _source_state(source)
        source_snapshot = hashlib.sha256(_json({path.name: sha for path, sha in source_state[1].items()}).encode()).hexdigest()
        ledger = CommunityEvidenceLedger(evidence_root=Path(evidence_root))
        with _read_without_side_effects(source) as reader, closing(sqlite3.connect(pending)) as writer:
            inspect_historical_schema(reader.cursor())
            reader.backup(writer)
            if _source_state(source) != source_state:
                raise StorageUpgradeError("source_changed_during_copy")
            writer.execute("PRAGMA journal_mode=DELETE")
            writer.execute("PRAGMA foreign_keys=ON")
            writer.execute("PRAGMA synchronous=FULL")
            install_verification_currentness_functions(writer)
            inspect_historical_schema(writer.cursor())
            preserved, seeds, added, changed = _convert_copy(writer, target=target, ledger=ledger,
                source_snapshot=source_snapshot)
        with pending.open("r+b") as stream:
            os.fsync(stream.fileno())
        if _source_state(source) != source_state:
            raise StorageUpgradeError("source_changed_during_copy")
        os.link(pending, output)
        pending.unlink()
        return {"contract_version": "verification-storage-upgrade-copy/v1", "source_release": "0.4.3",
            "target_release": "0.4.3", "source_schema_version": 406, "target_schema_version": 407,
            "source_schema_sha256": FORMAT406_SHA256, "target_schema_sha256": target.sha256,
            "source_snapshot_sha256": source_snapshot, "source_database_unchanged": True,
            "receipt_files_unchanged": True, "historical_actor_kind": "unknown",
            "preserved_tables": preserved, "preserved_tables_sha256": hashlib.sha256(_json(preserved).encode()).hexdigest(),
            "seed_summary": seeds, "objects_added": added, "objects_replaced": changed,
            "output_database": str(output), "installation_switched": False}
    except StorageFormatError as exc:
        raise StorageUpgradeError(exc.reason) from exc
    finally:
        pending.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm", "-journal"):
            pending.with_name(pending.name + suffix).unlink(missing_ok=True)
