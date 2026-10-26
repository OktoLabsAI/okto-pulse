"""SQLite source mutation hooks for atomic readiness invalidation.

The hooks run in the writer's transaction. A failed scope/job write therefore
aborts the source mutation too; no outbox worker or KG write is involved.
"""

from __future__ import annotations

_DIRECT = {
    "boards": "s.board_id = {row}.id",
    "specs": "s.id = {row}.id",
    "spec_history": "s.id = {row}.spec_id",
    "cards": "s.id = {row}.spec_id AND s.board_id = {row}.board_id",
    "delivery_evidence_records": (
        "s.id = {row}.spec_id AND s.board_id = {row}.board_id AND s.edition = {row}.edition"
    ),
    "card_delivery_evidence_records": (
        "s.id = {row}.spec_id AND s.board_id = {row}.board_id AND s.edition = {row}.spec_edition"
    ),
    "implementation_targets": "s.id = c.spec_id AND c.id = {row}.card_id AND c.board_id = {row}.board_id",
    "implementation_target_resolutions": "s.id = c.spec_id AND c.id = t.card_id AND t.id = {row}.target_id AND t.board_id = {row}.board_id",
    "implementation_target_execution_records": (
        "s.id = c.spec_id AND c.board_id = {row}.board_id AND (c.id = {row}.card_id "
        "OR c.id = (SELECT card_id FROM implementation_targets WHERE id = {row}.target_id))"
    ),
    "code_investigation_receipts": (
        "s.id = c.spec_id AND c.id = e.card_id AND e.result_investigation_receipt_id = {row}.id "
        "AND e.board_id = {row}.board_id"
    ),
    "code_investigation_receipt_revocations": (
        "s.id = c.spec_id AND c.id = e.card_id AND e.result_investigation_receipt_id = {row}.receipt_id "
        "AND e.board_id = {row}.board_id"
    ),
}


def _selection(table: str, row: str) -> str:
    predicate = _DIRECT[table].format(row=row)
    joins = {
        "implementation_targets": " JOIN cards c ON c.spec_id = s.id",
        "implementation_target_resolutions": " JOIN cards c ON c.spec_id = s.id JOIN implementation_targets t ON t.card_id = c.id",
        "implementation_target_execution_records": " JOIN cards c ON c.spec_id = s.id",
        "code_investigation_receipts": (
            " JOIN cards c ON c.spec_id = s.id JOIN implementation_target_execution_records e ON e.card_id = c.id"
        ),
        "code_investigation_receipt_revocations": (
            " JOIN cards c ON c.spec_id = s.id JOIN implementation_target_execution_records e ON e.card_id = c.id"
        ),
    }.get(table, "")
    return f"FROM specs s JOIN boards b ON b.id = s.board_id{joins} WHERE {predicate}"


def _statements(table: str, row: str, operation: str) -> str:
    selection = _selection(table, row)
    identity = f"SELECT s.id || ':' || s.edition {selection}"
    now = "strftime('%Y-%m-%d %H:%M:%f','now')"
    due = "strftime('%Y-%m-%d %H:%M:%f','now','+0.05 seconds')"
    if table == "card_delivery_evidence_records" and operation == "INSERT":
        delta_kind = "CASE WHEN NEW.kind='progress' THEN 'card_progress_insert' ELSE 'full' END"
        delta_card = "CASE WHEN NEW.kind='progress' THEN NEW.card_id ELSE NULL END"
    else:
        delta_kind, delta_card = "'full'", "NULL"
    return f"""
    INSERT OR IGNORE INTO readiness_scope
      (id,realm_id,board_id,spec_id,edition,source_revision,active_generation_id,tombstone,updated_at)
    SELECT s.id || ':' || s.edition,coalesce(b.realm_id,'local'),s.board_id,s.id,s.edition,
      0,NULL,0,{now} {selection};
    UPDATE readiness_scope SET source_revision=source_revision+1,updated_at={now},
      realm_id=(SELECT coalesce(realm_id,'local') FROM boards WHERE id=readiness_scope.board_id),
      tombstone=CASE WHEN EXISTS(SELECT 1 FROM specs active_spec
        WHERE active_spec.id=readiness_scope.spec_id
          AND active_spec.edition=readiness_scope.edition
          AND active_spec.archived=0) THEN 0 ELSE 1 END
      WHERE id IN ({identity});
    INSERT INTO readiness_source_delta (scope_id,source_revision,kind,card_id)
    SELECT id,source_revision,{delta_kind},{delta_card}
      FROM readiness_scope WHERE id IN ({identity})
    ON CONFLICT(scope_id) DO UPDATE SET
      source_revision=excluded.source_revision,kind=excluded.kind,card_id=excluded.card_id;
    INSERT INTO readiness_source_delta_event (scope_id,source_revision,kind,card_id)
    SELECT id,source_revision,{delta_kind},{delta_card}
      FROM readiness_scope WHERE id IN ({identity});
    INSERT INTO readiness_job
      (scope_id,requested_revision,state,owner,lease_epoch,lease_until,attempt,due_at,error_code,updated_at)
    SELECT id,source_revision,CASE WHEN tombstone=1 THEN 'terminal' ELSE 'pending' END,
      NULL,0,NULL,0,{due},CASE WHEN tombstone=1 THEN 'scope_tombstoned' ELSE NULL END,{now}
      FROM readiness_scope WHERE id IN ({identity})
    ON CONFLICT(scope_id) DO UPDATE SET
      requested_revision=max(readiness_job.requested_revision,excluded.requested_revision),
      state=CASE WHEN excluded.state='terminal' THEN 'terminal'
        WHEN readiness_job.state='running' THEN 'running' ELSE 'pending' END,
      owner=CASE WHEN excluded.state='terminal' THEN NULL
        WHEN readiness_job.state='running' THEN readiness_job.owner ELSE NULL END,
      lease_until=CASE WHEN excluded.state='terminal' THEN NULL
        WHEN readiness_job.state='running' THEN readiness_job.lease_until ELSE NULL END,
      attempt=CASE WHEN excluded.state='terminal' THEN 0
        WHEN readiness_job.state='running' THEN readiness_job.attempt ELSE 0 END,
      due_at=CASE WHEN readiness_job.state='pending' AND readiness_job.error_code IS NULL
        THEN min(readiness_job.due_at,excluded.due_at) ELSE excluded.due_at END,
      error_code=excluded.error_code,updated_at=excluded.updated_at;
    """


def readiness_trigger_statements() -> tuple[str, ...]:
    statements = []
    for table in _DIRECT:
        for operation in (("UPDATE",) if table == "boards" else ("INSERT", "UPDATE", "DELETE")):
            row = "OLD" if operation == "DELETE" else "NEW"
            # A deleted Spec is removed by foreign-key cascade; its old scope
            # must not be reintroduced with a dangling FK.
            if table == "specs" and operation == "DELETE":
                continue
            body = _statements(table, row, operation)
            if operation == "UPDATE" and table == "cards":
                # Reparenting invalidates both consumers, without double
                # counting ordinary field updates of the same Card.
                statements.append(f"""CREATE TRIGGER trg_readiness_cards_update_old AFTER UPDATE ON cards
                WHEN OLD.spec_id IS NOT NEW.spec_id OR OLD.board_id IS NOT NEW.board_id
                BEGIN {_statements(table, 'OLD', operation)} END""")
            if operation == "UPDATE" and table == "specs":
                statements.append("""CREATE TRIGGER trg_readiness_specs_update_old_edition AFTER UPDATE ON specs
                WHEN OLD.edition IS NOT NEW.edition
                BEGIN
                  UPDATE readiness_scope SET tombstone=1,updated_at=strftime('%Y-%m-%d %H:%M:%f','now')
                    WHERE id=OLD.id || ':' || OLD.edition;
                  UPDATE readiness_job SET state='terminal',owner=NULL,lease_until=NULL,error_code='edition_superseded'
                    WHERE scope_id=OLD.id || ':' || OLD.edition;
                END""")
            condition = (" WHEN OLD.settings IS NOT NEW.settings OR OLD.realm_id IS NOT NEW.realm_id"
                if table == "boards" else "")
            statements.append(f"CREATE TRIGGER trg_readiness_{table}_{operation.lower()} "
                f"AFTER {operation} ON {table}{condition} BEGIN {body} END")
    return tuple(statements)


def install_readiness_invalidation_triggers(connection) -> None:
    for statement in readiness_trigger_statements():
        connection.exec_driver_sql(statement)
