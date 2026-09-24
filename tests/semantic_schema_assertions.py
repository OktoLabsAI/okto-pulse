"""Physical semantic contracts shared by the offline predecessor qualifier."""

from okto_pulse.community.adapters import relational_schema_steps as steps
from okto_pulse.community.adapters import sqlalchemy_models as models


def exact_semantic_schema(connection):
    tables = (
        models.SemanticGuidelineRevisionRow,
        models.SemanticGuidelineBindingConfigurationRow,
        models.SemanticSubjectVersionEventRow,
        models.SemanticSubjectVersionRow,
        models.SemanticGuidelineAssessmentReceiptRow,
        models.SemanticGuidelineMetricResultRow,
        models.SemanticGuidelineFindingRow,
        models.SemanticGuidelineWaiverRow,
        models.SemanticGuidelineWaiverEventRow,
        models.SemanticGuidelineSkipRow,
        models.SemanticGuidelineLegacyMigrationRow,
        models.GuidelineBoardBindingRow,
    )
    for model in tables:
        contract = steps._sqlite_owned_table_contract(connection, model.__table__)
        assert contract['observed'] == contract['expected'], model.__tablename__
    ack_columns = tuple((str(row[1]), str(row[2]), int(row[3])) for row in
        connection.exec_driver_sql('PRAGMA table_xinfo(exact_rebuild_consolidation_ack_journal)'))
    assert ack_columns[10:13] == (
        ('membership_content_hash', 'VARCHAR(64)', 1),
        ('audit_content_hash', 'VARCHAR(64)', 1),
        ('consolidation_session_id', 'VARCHAR(36)', 1),
    )
    objects = tuple(tuple(row) for row in connection.exec_driver_sql(
        "SELECT type, name, tbl_name, sql FROM sqlite_schema "
        "WHERE name LIKE 'semantic_%' OR name LIKE 'trg_sgv3_%' "
        "OR name = 'uq_guideline_binding_exact_authority' "
        "OR name LIKE 'exact_rebuild_consolidation_%' "
        "OR name = 'ix_exact_rebuild_ack_scope' ORDER BY type, name"))
    assert objects
    assert {(kind, name) for kind, name, _, _ in objects if name.startswith('exact_rebuild_consolidation_')
        or name == 'ix_exact_rebuild_ack_scope'} == {
        ('index', 'ix_exact_rebuild_ack_scope'),
        ('table', 'exact_rebuild_consolidation_ack_journal'),
        ('table', 'exact_rebuild_consolidation_compensations'),
    }
    return objects
