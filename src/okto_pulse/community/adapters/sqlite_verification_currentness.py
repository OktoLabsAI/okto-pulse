"""Atomic scenario generations for every native SQLite writer.

These triggers belong to schema creation/verified conversion, never startup
repair. The function only projects facts; existing authenticated acceptance is
the sole exception to a non-result operational reset.
"""

from __future__ import annotations

import hashlib
import json

from okto_pulse.core.domain.verification_currentness import MAX_VERIFICATION_COUNTER
from okto_pulse.core.services.test_scenario_lifecycle import compute_test_scenario_semantic_sha256


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def scenario_facts(board_id, spec_id, scenarios_json, criteria_json, old_scenarios_json):
    scenarios = json.loads(scenarios_json or "null") or []
    criteria = json.loads(criteria_json or "null") or []
    old = json.loads(old_scenarios_json or "null") or []
    if not isinstance(scenarios, list) or not isinstance(criteria, list) or not isinstance(old, list):
        raise ValueError("verification_scenario_scope_invalid")
    previous = {item["id"]: item for item in old}
    seen, facts = set(), []
    for item in scenarios:
        identity = item["id"]
        if not isinstance(identity, str) or not identity.strip() or identity in seen:
            raise ValueError("verification_scenario_identity_invalid")
        seen.add(identity)
        try:
            digest = compute_test_scenario_semantic_sha256(board_id=board_id, spec_id=spec_id,
                scenario=item, acceptance_criteria=criteria).removeprefix("sha256:")
        except (TypeError, ValueError):
            # Removing a linked criterion may leave an unresolved editing state.
            # Persist its invalidation without pretending it is an executable
            # contract. Native reservation/read still reject the unresolved scope.
            digest = hashlib.sha256(_json(["unresolved-verification-scope/v1", board_id,
                spec_id, item, criteria]).encode("utf-8")).hexdigest()
        evidence = item.get("evidence")
        unsigned = {k: v for k, v in evidence.items() if k != "execution_receipt"} if isinstance(evidence, dict) else None
        before = previous.get(identity)
        status = item.get("status") or "draft"
        facts.append({"id": identity, "digest": digest,
            "method": item.get("verification_method") or "automated_test", "status": status,
            "receipt": evidence.get("execution_receipt") if isinstance(evidence, dict) else None,
            "evidence_digest": hashlib.sha256(_json(unsigned).encode("utf-8")).hexdigest() if unsigned is not None else None,
            "reset": before is not None and ((before.get("status") or "draft") != status
                or before.get("evidence") != evidence)})
    return _json(facts)


def install_verification_currentness_functions(connection) -> None:
    connection.create_function("pulse_verification_scenario_facts", 5, scenario_facts, deterministic=True)


def _increment(field):
    return (f"CASE WHEN {field}>={MAX_VERIFICATION_COUNTER} THEN "
            f"RAISE(ABORT,'verification_counter_exhausted') ELSE {field}+1 END")


def _upsert(old_scenarios):
    state = "verification_scenario_states"
    # Only the current authenticated global publication may change the summary
    # without resetting all bases. A copied old receipt or a caller's context
    # claim cannot satisfy this persisted acceptance/head/ordinal equality.
    published = f"""EXISTS(SELECT 1 FROM verification_acceptances a
      JOIN verification_basis_heads h ON h.accepted_result_id=a.id
      WHERE a.board_id=NEW.board_id AND a.spec_id=NEW.id AND a.spec_edition=NEW.edition
        AND a.scenario_id=json_extract(f.value,'$.id') AND a.generation={state}.generation
        AND a.ordinal={state}.summary_ordinal AND h.generation={state}.generation
        AND a.receipt_id=json_extract(f.value,'$.receipt')
        AND a.evidence_status=json_extract(f.value,'$.status')
        AND json_extract(a.result,'$.evidence_sha256')=json_extract(f.value,'$.evidence_digest'))"""
    facts = ("pulse_verification_scenario_facts(NEW.board_id,NEW.id,"
             f"NEW.test_scenarios,NEW.acceptance_criteria,{old_scenarios})")
    return f"""
    UPDATE {state} SET generation={_increment('generation')},last_ordinal=0,summary_ordinal=0,
      revision={_increment('revision')},scenario_sha256=json_extract(f.value,'$.digest'),
      verification_method=json_extract(f.value,'$.method'),deleted=0
    FROM (WITH facts AS MATERIALIZED (SELECT value FROM json_each({facts})) SELECT value FROM facts) f
    WHERE {state}.board_id=NEW.board_id AND {state}.spec_id=NEW.id AND {state}.spec_edition=NEW.edition
      AND {state}.scenario_id=json_extract(f.value,'$.id') AND (
        {state}.deleted=1 OR {state}.scenario_sha256 IS NOT json_extract(f.value,'$.digest')
        OR {state}.verification_method IS NOT json_extract(f.value,'$.method')
        OR (json_extract(f.value,'$.reset')=1 AND NOT {published}));
    INSERT INTO {state}
      (board_id,spec_id,spec_edition,scenario_id,generation,last_ordinal,summary_ordinal,
       revision,scenario_sha256,verification_method,deleted)
    SELECT NEW.board_id,NEW.id,NEW.edition,json_extract(f.value,'$.id'),1,0,0,1,
      json_extract(f.value,'$.digest'),json_extract(f.value,'$.method'),0
    FROM json_each({facts}) f WHERE NOT EXISTS(SELECT 1 FROM {state}
      WHERE board_id=NEW.board_id AND spec_id=NEW.id AND spec_edition=NEW.edition
        AND scenario_id=json_extract(f.value,'$.id'));
    """


def verification_currentness_trigger_statements():
    initial = _upsert("'[]'")
    tombstone = f"""UPDATE verification_scenario_states SET deleted=1,
      generation={_increment('generation')},revision={_increment('revision')},
      last_ordinal=0,summary_ordinal=0
      WHERE board_id=OLD.board_id AND spec_id=OLD.id AND spec_edition=OLD.edition AND deleted=0"""
    statements = [
        f"CREATE TRIGGER trg_verification_spec_insert AFTER INSERT ON specs BEGIN {initial} END",
        f"""CREATE TRIGGER trg_verification_spec_update
        AFTER UPDATE OF board_id,edition,test_scenarios,acceptance_criteria ON specs
        WHEN OLD.board_id IS NOT NEW.board_id OR OLD.edition IS NOT NEW.edition
          OR OLD.test_scenarios IS NOT NEW.test_scenarios OR OLD.acceptance_criteria IS NOT NEW.acceptance_criteria
        BEGIN
          {tombstone} AND (OLD.board_id IS NOT NEW.board_id OR OLD.edition IS NOT NEW.edition
            OR scenario_id NOT IN (SELECT json_extract(value,'$.id') FROM json_each(NEW.test_scenarios)));
          {_upsert('OLD.test_scenarios')}
        END""",
        f"CREATE TRIGGER trg_verification_spec_delete AFTER DELETE ON specs BEGIN {tombstone}; END",
    ]
    for table in ("verification_reservations", "verification_acceptances"):
        for operation in ("UPDATE", "DELETE"):
            statements.append(f"""CREATE TRIGGER trg_{table}_immutable_{operation.lower()}
              BEFORE {operation} ON {table} BEGIN
              SELECT RAISE(ABORT,'verification_history_immutable'); END""")
    return tuple(statements)


def install_verification_currentness_triggers(connection) -> None:
    for statement in verification_currentness_trigger_statements():
        connection.exec_driver_sql(statement)
