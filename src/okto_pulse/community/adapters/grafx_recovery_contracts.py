"""Exact predecessor contracts for offline backup/restore, never runtime writers.

Catalog shape selects a known recovery contract; the logical reader still checks
every column, vector space and endpoint. A BoardMeta label alone is not evidence
of that shape. Backing up historical metadata does not approve or upgrade it.
"""

from okto_pulse.core.kg.logical_transfer import (
    LogicalNodeType, LogicalRelationLayout, LogicalSchema, LogicalSchemaError,
    LogicalVectorSpace, schema_digest,
)

from .grafx_schema_evolution import PULSE_GRAFX_SCHEMA_MANIFEST as PREDECESSOR
from .grafx_schema_v060 import V060_MANIFEST, V060_FINGERPRINT
from .logical_transfer_factories import LogicalTransferScope, logical_transfer_scope
from .logical_transfer_schema import SchemaCensus, _property, require_no_schema_drift

_PREDECESSOR_FINGERPRINT = '4a7b425bf4b8c4864be633c1a87f034e5f7f641019dc029015b7d3ca786deb81'


def predecessor_recovery_contract():
    if PREDECESSOR.schema_version != '0.5.0' or PREDECESSOR.logical_fingerprint != _PREDECESSOR_FINGERPRINT:
        raise LogicalSchemaError('frozen predecessor recovery manifest changed')
    return _manifest_contract(PREDECESSOR, SchemaCensus(12, 69, 11, 489, 483))


def v060_recovery_contract():
    if V060_MANIFEST.schema_version != '0.6.0' or V060_MANIFEST.logical_fingerprint != V060_FINGERPRINT:
        raise LogicalSchemaError('frozen 0.6.0 recovery manifest changed')
    return _manifest_contract(V060_MANIFEST, SchemaCensus(12, 80, 11, 544, 560))


def _manifest_contract(manifest, census):

    def properties(table, *, relation=False):
        return tuple(_property(column.name, column.pulse_type,
            key=table.primary_key or '', vector_space=column.vector_space)
            for column in (table.columns[2:] if relation else table.columns))

    schema = LogicalSchema('board',
        node_types=tuple(LogicalNodeType(table.name, table.primary_key, properties(table))
            for table in (manifest.board_meta, *manifest.nodes)),
        relation_layouts=tuple(LogicalRelationLayout(table.logical_relationship, table.from_table, table.to_table,
            properties(table, relation=True)) for table in manifest.relationships),
        vector_spaces=tuple(LogicalVectorSpace(space.name, space.storage_dtype, space.dimension,
            space.metric, space.normalized) for space in manifest.spaces))
    require_no_schema_drift(schema, census)
    return LogicalTransferScope('board', schema, {
        (table.logical_relationship, table.from_table, table.to_table): table.name
        for table in manifest.relationships})


def _contracts(scope):
    current = logical_transfer_scope(scope)
    if scope != 'board':
        return (current,)
    unique = {}
    for contract in (current, v060_recovery_contract(), predecessor_recovery_contract()):
        digest = schema_digest(contract.schema)
        if digest in unique and unique[digest] != contract:
            raise LogicalSchemaError('recovery contract digest collision')
        unique[digest] = contract
    return tuple(unique.values())


def grafx_recovery_contract(database, *, scope):
    """Select a known offline contract; the caller must still validate columns."""
    observed = {(table.kind, table.name) for table in database.catalog.catalog.tables()}
    candidates = [contract for contract in _contracts(scope)
        if observed == ({('node', node.name) for node in contract.schema.node_types}
            | {('rel', table) for table in contract.relationship_tables.values()})]
    if len(candidates) != 1:
        raise LogicalSchemaError('unrecognized recovery catalog contract')
    return candidates[0]


def make_grafx_recovery_logical_source(database, *, scope, scan_batch_size=500, temporary_parent=None):
    from .logical_transfer_grafx import CommunityGrafxLogicalSnapshotSource

    contract = grafx_recovery_contract(database, scope=scope)
    return CommunityGrafxLogicalSnapshotSource(database, schema=contract.schema,
        relationship_tables=contract.relationship_tables, scan_batch_size=scan_batch_size,
        temporary_parent=temporary_parent)


def make_grafx_recovery_logical_sink(candidate_path, *, scope, expected_schema_digest, max_batch_size=500):
    from .grafx_logical_sink import CommunityGrafxLogicalCandidateSink

    candidates = [contract for contract in _contracts(scope) if schema_digest(contract.schema) == expected_schema_digest]
    if len(candidates) != 1:
        raise LogicalSchemaError('unrecognized recovery artifact contract')
    contract = candidates[0]
    return CommunityGrafxLogicalCandidateSink(candidate_path, expected_schema=contract.schema,
        relationship_tables=contract.relationship_tables, max_batch_size=max_batch_size)
