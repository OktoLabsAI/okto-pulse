"""SQLite change tokens for planning fences and visible workspace metadata.

The closed census below follows relational inputs, not projection queues or
activity/audit traffic. Triggers run for ORM and direct SQL, in the source
transaction. Reads need only indexed revision rows, not full payload hashes.
No-op lock UPDATEs and routine agent last_used_at updates do not advance them.
"""

from dataclasses import dataclass

from sqlalchemy import MetaData


@dataclass(frozen=True)
class RevisionSource:
    table: str
    targets: tuple[str, ...]
    columns: tuple[str, ...] | None = None


def _direct(kind: str, column: str) -> str:
    return f"SELECT '{kind}' AS scope_kind, ROW.{column} AS scope_id"


def _card(column: str) -> tuple[str, ...]:
    return (
        _direct("card", column),
        f"SELECT 'spec', spec_id FROM cards WHERE id = ROW.{column}",
    )


def _subject(type_column: str = "subject_type", id_column: str = "subject_id") -> tuple[str, ...]:
    return (
        f"SELECT 'spec' AS scope_kind, ROW.{id_column} AS scope_id WHERE ROW.{type_column} = 'spec'",
        f"SELECT 'card', ROW.{id_column} WHERE ROW.{type_column} IN ('card', 'task', 'test', 'bug')",
        f"SELECT 'spec', spec_id FROM cards WHERE id = ROW.{id_column} AND ROW.{type_column} IN ('card', 'task', 'test', 'bug')",
    )


def validation_revision_sources() -> tuple[RevisionSource, ...]:
    """Every entry names a concrete Community input and its affected subjects."""
    sources = [
        RevisionSource("boards", (_direct("board", "id"),), ("id", "owner_id", "realm_id", "settings")),
        RevisionSource("specs", (_direct("spec", "id"),)),
        RevisionSource("cards", (*_card("id"), _direct("spec", "spec_id"))),
    ]
    for table in (
        "architecture_candidate_decisions", "spec_qa_items", "spec_knowledge_bases",
        "checklist_validation_binding_snapshots", "checklist_executions",
        "checklist_receipts", "checklist_execution_heads", "requirement_lint_validation_snapshots",
        "code_evidence_spec_links", "code_evidence_dispositions", "delivery_evidence_records",
        "research_decision_derivations", "implementation_target_spec_links",
    ):
        sources.append(RevisionSource(table, (_direct("spec", "spec_id"),)))
    sources.append(RevisionSource("card_delivery_evidence_records", (
        _direct("spec", "spec_id"), *_card("card_id"),
    )))
    for table in (
        "qa_items", "attachments", "implementation_targets",
        "implementation_target_execution_records",
    ):
        sources.append(RevisionSource(table, _card("card_id")))
    sources.append(RevisionSource("card_dependencies", (*_card("card_id"), *_card("depends_on_id"))))
    sources.append(RevisionSource("spec_dependencies", (
        _direct("spec", "dependent_spec_id"), _direct("spec", "prerequisite_spec_id"),
    )))
    for table in (
        "quality_assessment_heads", "quality_assessment_receipts", "quality_findings", "semantic_subject_versions",
        "semantic_guideline_assessments_v2", "semantic_guideline_validation_scopes",
        "semantic_guideline_metric_results_v2", "semantic_guideline_findings_v2",
        "semantic_guideline_waivers", "semantic_guideline_skips", "code_investigation_receipts",
    ):
        sources.append(RevisionSource(table, _subject()))
    sources.append(RevisionSource("resource_not_applicable", _subject("entity_type", "entity_id")))
    # A spec_entity waiver addresses a requirement rather than a Spec ID.
    sources.append(RevisionSource("code_traceability_waivers", (
        *_subject("entity_type", "entity_id"),
        "SELECT 'board', ROW.board_id WHERE ROW.entity_type='spec_entity'",
    )))
    for table in ("architecture_designs", "code_evidence"):
        sources.append(RevisionSource(table, (_direct("spec", "spec_id"), *_card("card_id"))))
    for table in ("architecture_diagram_payloads", "architecture_finding_runs", "architecture_findings", "architecture_warning_acknowledgements"):
        sources.append(RevisionSource(table, (
            "SELECT 'spec' AS scope_kind, spec_id AS scope_id FROM architecture_designs WHERE id = ROW.design_id",
            "SELECT 'card', card_id FROM architecture_designs WHERE id = ROW.design_id",
            "SELECT 'spec', c.spec_id FROM cards c JOIN architecture_designs d ON d.card_id=c.id WHERE d.id=ROW.design_id",
        )))
    for table in ("implementation_target_resolutions", "implementation_target_evidence_links"):
        sources.append(RevisionSource(table, (
            "SELECT 'card' AS scope_kind, card_id AS scope_id FROM implementation_targets WHERE id=ROW.target_id",
            "SELECT 'spec', c.spec_id FROM cards c JOIN implementation_targets t ON t.card_id=c.id WHERE t.id=ROW.target_id",
        )))
    sources.append(RevisionSource("target_overlap_acknowledgements", (
        "SELECT 'card' AS scope_kind, card_id AS scope_id FROM implementation_targets WHERE id IN (ROW.target_a_id,ROW.target_b_id)",
        "SELECT 'spec', c.spec_id FROM cards c JOIN implementation_targets t ON t.card_id=c.id WHERE t.id IN (ROW.target_a_id,ROW.target_b_id)",
    )))
    sources.append(RevisionSource("checklist_item_results", (
        "SELECT 'spec' AS scope_kind, spec_id AS scope_id FROM checklist_receipts WHERE id=ROW.receipt_id",
    )))
    sources.append(RevisionSource("semantic_guideline_waiver_events", (
        "SELECT 'spec' AS scope_kind, subject_id AS scope_id FROM semantic_guideline_waivers WHERE waiver_id=ROW.waiver_id AND subject_type='spec'",
        "SELECT 'card', subject_id FROM semantic_guideline_waivers WHERE waiver_id=ROW.waiver_id AND subject_type IN ('card','task','test','bug')",
        "SELECT 'spec', c.spec_id FROM cards c JOIN semantic_guideline_waivers w ON w.subject_id=c.id WHERE w.waiver_id=ROW.waiver_id AND w.subject_type IN ('card','task','test','bug')",
    )))
    # Effective board authority is shared by its Specs. Changes to a shared
    # source (code head, guideline or permission grant) must invalidate all its
    # consumers, including consumers whose Spec.version did not change.
    for table in (
        "board_shares", "agent_boards", "board_guidelines", "board_design_systems",
        "guideline_board_bindings", "semantic_guideline_binding_configurations",
        "checklist_bindings", "checklist_binding_heads", "code_investigation_heads",
        "code_investigation_receipt_revocations",
    ):
        sources.append(RevisionSource(table, (_direct("board", "board_id"),)))
    agent_boards = (
        _direct("board", "board_id"),
        "SELECT 'board', board_id FROM agent_boards WHERE agent_id=ROW.id",
    )
    sources.append(RevisionSource("agents", agent_boards, (
        "id", "board_id", "is_active", "permission_flags", "preset_id", "api_key", "api_key_hash",
    )))
    # Preset flags are materialized by the native permission service; no
    # implicit inheritance is introduced here. Cover its dependent presets as
    # well so changes to a base cannot evade the freshness boundary.
    sources.append(RevisionSource("permission_presets", (
        "SELECT 'board' AS scope_kind, board_id AS scope_id FROM agents WHERE preset_id IN (WITH RECURSIVE affected(id) AS (SELECT ROW.id UNION SELECT p.id FROM permission_presets p JOIN affected a ON p.base_preset_id=a.id) SELECT id FROM affected)",
        "SELECT 'board', g.board_id FROM agent_boards g JOIN agents a ON a.id=g.agent_id WHERE a.preset_id IN (WITH RECURSIVE affected(id) AS (SELECT ROW.id UNION SELECT p.id FROM permission_presets p JOIN affected a ON p.base_preset_id=a.id) SELECT id FROM affected)",
    ), ("id", "flags", "base_preset_id")))
    for table, key in (("guidelines", "id"), ("guideline_heads", "guideline_id"), ("guideline_retirements", "guideline_id"), ("semantic_guideline_revisions", "guideline_id")):
        sources.append(RevisionSource(table, (
            f"SELECT 'board' AS scope_kind, board_id AS scope_id FROM guideline_board_bindings WHERE guideline_id=ROW.{key}",
            f"SELECT 'board', board_id FROM board_guidelines WHERE guideline_id=ROW.{key}",
        )))
    sources.append(RevisionSource("design_systems", (
        "SELECT 'board' AS scope_kind, board_id AS scope_id FROM board_design_systems WHERE design_system_id=ROW.id",
    )))
    sources.append(RevisionSource("knowledge_propagation_scopes", _subject("target_type", "target_id")))
    for table in ("knowledge_propagation_assignments", "knowledge_propagation_snapshots", "knowledge_propagation_tombstones"):
        sources.append(RevisionSource(table, (
            "SELECT 'spec' AS scope_kind, target_id AS scope_id FROM knowledge_propagation_scopes WHERE id=ROW.scope_id AND target_type='spec'",
            "SELECT 'card', target_id FROM knowledge_propagation_scopes WHERE id=ROW.scope_id AND target_type IN ('card','task','test','bug')",
            "SELECT 'spec', c.spec_id FROM cards c JOIN knowledge_propagation_scopes s ON s.target_id=c.id WHERE s.id=ROW.scope_id AND s.target_type IN ('card','task','test','bug')",
        )))
    return tuple(sources)


def validation_revision_trigger_manifest(metadata: MetaData) -> dict[str, tuple[str, str]]:
    manifest: dict[str, tuple[str, str]] = {}
    for source in validation_revision_sources():
        table = metadata.tables[source.table]
        columns = source.columns or tuple(column.name for column in table.columns if column.name != "updated_at")
        for operation, aliases in (("insert", ("NEW",)), ("update", ("OLD", "NEW")), ("delete", ("OLD",))):
            name = f"validation_revision_{source.table}_{operation}"
            targets = " UNION ".join(query.replace("ROW.", f"{alias}.") for alias in aliases for query in source.targets)
            changed = " OR ".join(f'NEW."{column}" IS NOT OLD."{column}"' for column in columns)
            when = f"WHEN {changed}" if operation == "update" else ""
            root = {"boards": "board", "specs": "spec", "cards": "card"}.get(source.table)
            cleanup = (
                f"DELETE FROM validation_source_revisions WHERE scope_kind='{root}' AND scope_id=OLD.id;"
                if operation == "delete" and root else ""
            )
            sql = f'''CREATE TRIGGER "{name}" AFTER {operation.upper()} ON "{source.table}"
{when}
BEGIN
  INSERT INTO validation_source_revisions(scope_kind,scope_id,incarnation,revision)
  SELECT scope_kind,scope_id,lower(hex(randomblob(16))),1 FROM ({targets})
  WHERE scope_id IS NOT NULL AND scope_id <> ''
  ON CONFLICT(scope_kind,scope_id) DO UPDATE SET revision=revision+1;
  {cleanup}
END'''
            manifest[name] = (source.table, sql)
    # A deleted subject needs no retained metadata. Re-creation gets a fresh
    # incarnation, so it cannot accidentally accept a prior request's token.
    manifest["validation_revision_no_delete"] = ("validation_source_revisions", '''CREATE TRIGGER validation_revision_no_delete
BEFORE DELETE ON validation_source_revisions
WHEN (OLD.scope_kind='board' AND EXISTS(SELECT 1 FROM boards WHERE id=OLD.scope_id))
OR (OLD.scope_kind='spec' AND EXISTS(SELECT 1 FROM specs WHERE id=OLD.scope_id))
OR (OLD.scope_kind='card' AND EXISTS(SELECT 1 FROM cards WHERE id=OLD.scope_id))
BEGIN SELECT RAISE(ABORT,'validation_revision_delete_forbidden'); END''')
    manifest["validation_revision_monotonic"] = ("validation_source_revisions", '''CREATE TRIGGER validation_revision_monotonic
BEFORE UPDATE ON validation_source_revisions
WHEN NEW.scope_kind IS NOT OLD.scope_kind OR NEW.scope_id IS NOT OLD.scope_id
OR NEW.incarnation IS NOT OLD.incarnation OR NEW.revision <> OLD.revision+1
BEGIN SELECT RAISE(ABORT,'validation_revision_invalid_advance'); END''')
    return manifest


def install_validation_revision_triggers(connection, metadata: MetaData) -> None:
    """Create only with a fresh schema; no backfill or runtime reconciliation."""
    for _, sql in validation_revision_trigger_manifest(metadata).values():
        connection.exec_driver_sql(sql)
