"""Board-scoped census of historical polymorphic Sprint references.

This is an internal migration read model, not an archive or an authorization
decision. In particular an anchor's Board never replaces its row owner's Board.
Rows can occur under multiple reference roles; an archive must deduplicate by
physical table/primary key, preserving the original row once.
"""

from dataclasses import dataclass

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection


@dataclass(frozen=True, slots=True)
class _Root:
    table: str
    discriminator: str
    identity: str
    owner_column: str = "board_id"
    target_column: str = "board_id"
    parent: tuple[str, str] | None = None  # owner table, local foreign key


_ROOTS = (
    *(_Root(table, "entity_type", "entity_id") for table in (
        "resource_not_applicable", "guideline_impact_items", "design_system_gate_audit",
        "code_evidence_spec_links", "code_traceability_waivers",
    )),
    *(_Root(table, "entity_type", "subject_id") for table in (
        "policy_compliance_receipts", "policy_compliance_findings", "policy_waivers",
    )),
    *(_Root(table, "subject_type", "subject_id") for table in (
        "semantic_subject_version_events", "semantic_subject_versions", "semantic_guideline_validation_scopes",
        "semantic_guideline_assessment_receipts", "semantic_guideline_metric_results", "semantic_guideline_findings",
        "semantic_guideline_waivers", "semantic_guideline_skips", "quality_assessment_receipts", "quality_findings",
        "quality_assessment_heads", "quality_assessment_subject_erasure_permits", "quality_assessment_legacy_import_candidates",
        "quality_assessment_lifecycle_transitions", "quality_assessment_lifecycle_stale_transitions",
        "semantic_guideline_assessments_v2", "semantic_guideline_metric_results_v2", "semantic_guideline_findings_v2",
        "code_investigation_requests", "code_investigation_receipts",
    )),
    *(_Root(table, "artifact_type", "artifact_id") for table in (
        "consolidation_queue", "artifact_deletion_tombstones", "canonical_debt", "consolidation_dead_letter",
        "consolidation_audit", "exact_rebuild_consolidation_ack_journal", "global_discovery_delivery_ledger",
        "kg_takedown_state_events",
    )),
    _Root("agent_seen_items", "item_type", "item_id"),
    _Root("semantic_guideline_legacy_migrations", "source_type", "source_id"),
    _Root("quality_findings", "anchor_subject_type", "anchor_subject_id", target_column="anchor_board_id"),
    _Root("quality_assessment_legacy_import_checkpoints", "last_subject_type", "last_subject_id"),
    _Root("implementation_target_spec_links", "entity_type", "entity_id", parent=("specs", "spec_id")),
    _Root("ideation_knowledge_bases", "source_type", "source_id", parent=("ideations", "ideation_id")),
    _Root("refinement_knowledge_bases", "source_type", "source_id", parent=("refinements", "refinement_id")),
    _Root("spec_knowledge_bases", "source_type", "source_id", parent=("specs", "spec_id")),
)
# These children have no subject discriminator. Their ownership comes from the
# explicitly named receipt/waiver, not from a guessed common column suffix.
_CHILDREN = (
    ("policy_compliance_adopted_revisions", "receipt_id", "policy_compliance_receipts", "receipt_id"),
    ("policy_waiver_events", "waiver_id", "policy_waivers", "waiver_id"),
    ("semantic_guideline_waiver_events", "waiver_id", "semantic_guideline_waivers", "waiver_id"),
    ("quality_proposed_questions", "receipt_id", "quality_assessment_receipts", "id"),
    ("quality_finding_qa_links", "finding_id", "quality_findings", "id"),
    ("quality_assessment_outbox", "receipt_id", "quality_assessment_receipts", "id"),
    ("code_investigation_receipt_revocations", "receipt_id", "code_investigation_receipts", "id"),
    ("code_investigation_heads", "current_receipt_id", "code_investigation_receipts", "id"),
    ("code_investigation_heads", "latest_receipt_id", "code_investigation_receipts", "id"),
)


@dataclass(frozen=True, slots=True)
class SprintHistoricalReference:
    table: str
    key: tuple[tuple[str, object], ...]
    role: str
    sprint_id: str | None
    owner_board_id: str | None
    reference_board_id: str | None
    scope_state: str


@dataclass(frozen=True, slots=True)
class SprintReferenceInventory:
    counts: tuple[tuple[str, int], ...]
    references: tuple[SprintHistoricalReference, ...]

    def require_resolved_scopes(self) -> None:
        unresolved = tuple(item for item in self.references if item.scope_state not in {
            "current_source", "historical_source_absent",
        })
        if unresolved:
            error = SprintReferenceInspectionError(f"sprint_retirement_reference_scope_review:{len(unresolved)}")
            error.references = unresolved
            raise error


class SprintReferenceInspectionError(RuntimeError):
    references: tuple[SprintHistoricalReference, ...] = ()


def inspect_sprint_historical_references(connection: Connection, *, remaining_rows: int) -> SprintReferenceInventory:
    schema = inspect(connection)
    tables = set(schema.get_table_names())
    columns = {table: {c["name"] for c in schema.get_columns(table)} for table in sorted(tables)}
    classified = {(root.table, root.discriminator) for root in _ROOTS}
    discriminators = {root.discriminator for root in _ROOTS}
    for table, names in columns.items():
        for discriminator in names & discriminators:
            if (table, discriminator) not in classified:
                raise SprintReferenceInspectionError(f"sprint_retirement_unclassified_subject_column:{table}.{discriminator}")
    quote = connection.dialect.identifier_preparer.quote
    roots = {root.table: root for root in _ROOTS if root.discriminator != "anchor_subject_type"}
    counts = {}
    references = []
    consumed = 0

    def require(table, names):
        if table not in columns or not set(names) <= columns[table]:
            raise SprintReferenceInspectionError(f"sprint_retirement_reference_schema_incomplete:{table}")

    def owner(root):
        if root.parent is None:
            require(root.table, (root.owner_column, root.target_column))
            return f"r.{quote(root.owner_column)}", f"r.{quote(root.target_column)}", ""
        table, foreign_key = root.parent
        require(root.table, (foreign_key,))
        require(table, ("id", "board_id"))
        return "parent.board_id", "parent.board_id", f" LEFT JOIN {quote(table)} parent ON parent.id=r.{quote(foreign_key)}"

    def collect(root, *, child=None):
        nonlocal consumed
        require(root.table, (root.discriminator, root.identity))
        owner_sql, target_sql, parent_join = owner(root)
        table = root.table if child is None else child[0]
        role = root.discriminator if child is None else f"via:{root.table}.{child[1]}"
        alias = "r" if child is None else "child"
        require(table, ())
        key_columns = tuple(schema.get_pk_constraint(table).get("constrained_columns") or ())
        if not key_columns:
            raise SprintReferenceInspectionError(f"sprint_retirement_reference_key_missing:{table}")
        projection = ", ".join(f"{alias}.{quote(key)} AS {quote('key_' + str(i))}" for i, key in enumerate(key_columns))
        join = ""
        parent_owner_sql = owner_sql
        if child is not None:
            _, foreign_key, _, parent_key = child
            require(table, (foreign_key,))
            require(root.table, (parent_key,))
            constraints = [fk for fk in schema.get_foreign_keys(table)
                if fk["referred_table"] == root.table
                and (foreign_key, parent_key) in zip(fk["constrained_columns"], fk["referred_columns"], strict=True)]
            if not constraints:
                raise SprintReferenceInspectionError(f"sprint_retirement_history_fk_missing:{table}.{foreign_key}")
            # A regular JOIN would erase orphans from the census. Validate the
            # actual composite relation first, including Board/receipt scope.
            # MATCH SIMPLE permits nullable links; do not turn those into orphans.
            for constraint in constraints:
                pairs = tuple(zip(constraint["constrained_columns"], constraint["referred_columns"], strict=True))
                relation = " AND ".join(f"child.{quote(local)}=r.{quote(remote)}" for local, remote in pairs)
                nonnull = " AND ".join(f"child.{quote(local)} IS NOT NULL" for local, _ in pairs)
                owner_projection = "child.board_id" if "board_id" in columns[table] else "NULL"
                invalid_query = text(f"""
                    SELECT {projection}, {owner_projection} AS owner_board_id
                    FROM {quote(table)} child LEFT JOIN {quote(root.table)} r ON {relation}
                    WHERE {nonnull} AND r.{quote(parent_key)} IS NULL
                    ORDER BY {', '.join(f'child.{quote(key)}' for key in key_columns)} LIMIT 1
                """)
                with connection.execute(invalid_query) as invalid:
                    row = invalid.mappings().first()
                    if row is not None:
                        error = SprintReferenceInspectionError(f"sprint_retirement_history_parent_invalid:{table}.{foreign_key}")
                        error.references = (SprintHistoricalReference(table,
                            tuple((key, row[f"key_{i}"]) for i, key in enumerate(key_columns)), role, None,
                            row["owner_board_id"], None, "history_parent_invalid"),)
                        raise error
            join = f" JOIN {quote(table)} child ON child.{quote(foreign_key)}=r.{quote(parent_key)}"
            if "board_id" in columns[table]:
                owner_sql = "child.board_id"
        query = f"""
            SELECT {projection}, r.{quote(root.identity)} AS sprint_id,
                   {owner_sql} AS owner_board_id, {target_sql} AS reference_board_id,
                   {parent_owner_sql} AS parent_owner_board_id,
                   owner_board.id AS owner_exists, reference_board.id AS reference_exists,
                   s.id AS sprint_exists, s.board_id AS sprint_board_id
            FROM {quote(root.table)} r {parent_join} {join}
            LEFT JOIN boards owner_board ON owner_board.id={owner_sql}
            LEFT JOIN boards reference_board ON reference_board.id={target_sql}
            LEFT JOIN sprints s ON s.id=r.{quote(root.identity)}
            WHERE r.{quote(root.discriminator)}=:subject_type
            ORDER BY {', '.join(f'{alias}.{quote(key)}' for key in key_columns)} LIMIT :row_limit
        """
        count_key = f"{table}:{role}"
        counts[count_key] = 0
        statement = text(query).execution_options(stream_results=True, yield_per=16)
        with connection.execute(statement, {"subject_type": "sprint", "row_limit": remaining_rows - consumed + 1}) as result:
            for row in result.mappings():
                consumed += 1
                if consumed > remaining_rows:
                    raise SprintReferenceInspectionError("sprint_retirement_reference_row_limit")
                counts[count_key] += 1
                state = "current_source" if row["sprint_exists"] is not None else "historical_source_absent"
                if root.table == "quality_findings":
                    # The current physical contract permits only ideation,
                    # refinement and spec subjects/anchors. A legacy/drifted
                    # Sprint row needs investigation, never legitimization.
                    state = "unsupported_subject_contract"
                elif not row["sprint_id"] or row["reference_exists"] is None:
                    state = "reference_scope_missing"
                elif row["owner_exists"] is None:
                    state = "owner_scope_missing"
                elif child is not None and row["owner_board_id"] != row["parent_owner_board_id"]:
                    state = "child_owner_mismatch"
                elif row["sprint_exists"] is not None and row["sprint_board_id"] != row["reference_board_id"]:
                    state = "cross_board_reference"
                references.append(SprintHistoricalReference(table,
                    tuple((key, row[f"key_{i}"]) for i, key in enumerate(key_columns)), role,
                    row["sprint_id"], row["owner_board_id"], row["reference_board_id"], state))

    for root in _ROOTS:
        collect(root)
    for child in _CHILDREN:
        collect(roots[child[2]], child=child)
    return SprintReferenceInventory(tuple(sorted(counts.items())), tuple(references))
