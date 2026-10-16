"""Recovery accepts exactly the current logical contract, without conversion."""
from okto_pulse.core.kg.logical_transfer import LogicalSchemaError, schema_digest
from .logical_transfer_factories import logical_transfer_scope

def grafx_recovery_contract(database, *, scope):
    contract = logical_transfer_scope(scope)
    observed = {(table.kind, table.name) for table in database.catalog.catalog.tables()}
    expected = ({('node', node.name) for node in contract.schema.node_types}
                | {('rel', table) for table in contract.relationship_tables.values()})
    if observed != expected:
        raise LogicalSchemaError('incompatible recovery catalog contract')
    if scope == 'board':
        from .grafx_schema_bootstrap import read_current_grafx_schema_version
        from .grafx_schema_manifest import PULSE_GRAFX_SCHEMA_MANIFEST

        if read_current_grafx_schema_version(database) != PULSE_GRAFX_SCHEMA_MANIFEST.schema_version:
            raise LogicalSchemaError('incompatible recovery schema version')
    return contract

def make_grafx_recovery_logical_source(database, *, scope, scan_batch_size=500, temporary_parent=None):
    from .logical_transfer_grafx import CommunityGrafxLogicalSnapshotSource
    contract = grafx_recovery_contract(database, scope=scope)
    return CommunityGrafxLogicalSnapshotSource(database, schema=contract.schema,
        relationship_tables=contract.relationship_tables, scan_batch_size=scan_batch_size,
        temporary_parent=temporary_parent)

def make_grafx_recovery_logical_sink(candidate_path, *, scope, expected_schema_digest, max_batch_size=500):
    from .grafx_logical_sink import CommunityGrafxLogicalCandidateSink
    contract = logical_transfer_scope(scope)
    if schema_digest(contract.schema) != expected_schema_digest:
        raise LogicalSchemaError('incompatible recovery artifact contract')
    return CommunityGrafxLogicalCandidateSink(candidate_path, expected_schema=contract.schema,
        relationship_tables=contract.relationship_tables, max_batch_size=max_batch_size)
