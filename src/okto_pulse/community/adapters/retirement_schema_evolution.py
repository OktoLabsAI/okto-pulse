"""Build an unbound 0.6.0 graph from an authenticated 0.5.0 recovery snapshot.

Only the frozen additive schema delta is applied. Native history remains in the
authenticated predecessor backup; logical import does not claim its UUID/cursors.
This internal step neither publishes bindings nor authorizes runtime admission.
"""

from dataclasses import asdict, replace
from pathlib import Path

from okto_pulse.core.kg.logical_transfer import (
    LOGICAL_NULL, LogicalFingerprintAccumulator, LogicalSchemaError,
    schema_digest, transfer_logical_graph,
)

from .grafx_recovery_contracts import predecessor_recovery_contract
from .grafx_schema_manifest import PULSE_GRAFX_SCHEMA_MANIFEST
from .joint_recovery_snapshot import RecoveryBuildPair, verify_joint_recovery_snapshot
from .logical_graph_transfer import LogicalGraphFileSnapshotSource
from .logical_transfer_factories import logical_transfer_scope, make_grafx_logical_sink
from .relational_recovery_snapshot import _check_time, _deadline, _digest

_TARGET_FINGERPRINT = '3ab6faf0fd8a7fe3694ed7ddd336faa97a6b4af4a1626aafe20c75b0922b2bbe'
_INTRODUCED = frozenset({'severity', 'source_status', 'source_created_at', 'source_updated_at', 'resolved_at'})


def _schemas():
    previous = predecessor_recovery_contract().schema
    current = logical_transfer_scope('board').schema
    if (PULSE_GRAFX_SCHEMA_MANIFEST.schema_version != '0.6.0'
            or PULSE_GRAFX_SCHEMA_MANIFEST.logical_fingerprint != _TARGET_FINGERPRINT):
        raise LogicalSchemaError('retirement schema target contract changed')
    if previous.vector_spaces != current.vector_spaces or {node.name for node in previous.node_types} != {node.name for node in current.node_types}:
        raise LogicalSchemaError('retirement schema nonadditive node or vector delta')
    for node in previous.node_types:
        target = current.node_type(node.name)
        if node.key != target.key or any(target.property_def(prop.name) != prop for prop in node.properties):
            raise LogicalSchemaError('retirement schema changed predecessor property')
        added = target.property_names() - node.property_names()
        if added != (set() if node.name == 'BoardMeta' else _INTRODUCED):
            raise LogicalSchemaError('retirement schema unexpected property delta')
    for layout in previous.relation_layouts:
        if current.relation_layout(*layout.identity) != layout:
            raise LogicalSchemaError('retirement schema changed predecessor relation')
    if len(current.relation_layouts) - len(previous.relation_layouts) != 11:
        raise LogicalSchemaError('retirement schema unexpected relation delta')
    return previous, current


class _EvolutionSnapshot:
    def __init__(self, source, *, previous, current, board_id, certificate, source_path, source_sha256, deadline):
        self.source, self.current = source, current
        self.board_id, self.certificate = board_id, certificate
        self.source_path, self.source_sha256, self.deadline = source_path, source_sha256, deadline
        if source.schema() != previous or asdict(source.counts()) != certificate['counts']:
            raise LogicalSchemaError('retirement schema predecessor artifact changed')
        self.measured = LogicalFingerprintAccumulator.for_schema(previous)
        self.metadata_count = 0

    def schema(self):
        return self.current

    def counts(self):
        old = self.source.counts()
        if old.nodes < 1:
            raise LogicalSchemaError('retirement schema board metadata missing')
        return replace(old, properties=old.properties + len(_INTRODUCED) * (old.nodes - 1))

    def iter_nodes(self, *, batch_size):
        for batch in self.source.iter_nodes(batch_size=batch_size):
            _check_time(self.deadline)
            transformed = []
            for node in batch:
                self.measured.add_node(node)
                if node.type_name == 'BoardMeta':
                    self.metadata_count += 1
                    if (self.metadata_count != 1 or node.key != self.board_id
                            or node.properties.get('schema_version') != '0.5.0'):
                        raise LogicalSchemaError('retirement schema board metadata ambiguous')
                    properties = {**node.properties, 'schema_version': '0.6.0'}
                else:
                    properties = {**node.properties, **{name: LOGICAL_NULL for name in _INTRODUCED}}
                transformed.append(replace(node, properties=properties))
            yield tuple(transformed)
        if self.metadata_count != 1:
            raise LogicalSchemaError('retirement schema board metadata missing')

    def iter_relations(self, *, batch_size):
        for batch in self.source.iter_relations(batch_size=batch_size):
            _check_time(self.deadline)
            for relation in batch:
                self.measured.add_relation(relation)
            yield batch  # Preserve every occurrence, including parallel edges.
        if (not self.source.manifest_verified or self.measured.schema_hex != self.certificate['schema_digest']
                or self.measured.digest() != self.certificate['fingerprint']
                or asdict(self.measured.counts()) != self.certificate['counts']
                or _digest(self.source_path) != self.source_sha256):
            raise LogicalSchemaError('retirement schema predecessor proof changed')
        _check_time(self.deadline)

    def close(self):
        self.source.close()


class _EvolutionSource:
    def __init__(self, source_path, **options):
        self.source_path, self.options = source_path, options

    def open_snapshot(self):
        source = LogicalGraphFileSnapshotSource(self.source_path).open_snapshot()
        try:
            return _EvolutionSnapshot(source, source_path=self.source_path, **self.options)
        except BaseException:
            source.close()
            raise


def build_retirement_v060_graph(snapshot, target, *, board_id, builds, max_seconds=180, batch_size=500):
    """Apply only the frozen schema delta; caller owns offline publication fences."""
    deadline = _deadline(max_seconds)
    manifest = verify_joint_recovery_snapshot(snapshot, max_seconds=max_seconds)
    if type(builds) is not RecoveryBuildPair or asdict(builds) != manifest['builds']:
        raise ValueError('retirement_schema_build_pair_mismatch')
    if 'native_graphs' not in manifest:
        raise ValueError('retirement_schema_native_history_backup_required')
    selected = [(index, graph) for index, graph in enumerate(manifest['graphs'])
        if graph['scope'] == 'board' and graph['board_id'] == board_id]
    if len(selected) != 1:
        raise ValueError('retirement_schema_board_ambiguous')
    index, graph = selected[0]
    previous, current = _schemas()
    if graph['certificate']['schema_digest'] != schema_digest(previous):
        raise ValueError('retirement_schema_predecessor_required')
    target = Path(target).resolve()
    for protected in (snapshot.directory.resolve(), Path(graph['source_path']).resolve()):
        if target == protected or target in protected.parents or protected in target.parents:
            raise ValueError('retirement_schema_target_overlaps_source')
    source = _EvolutionSource(snapshot.directory / graph['file'], previous=previous, current=current,
        board_id=board_id, certificate=graph['certificate'], source_sha256=graph['sha256'], deadline=deadline)
    report = transfer_logical_graph(source, make_grafx_logical_sink(target, scope='board', max_batch_size=batch_size),
        batch_size=batch_size)
    return {'format': 'retirement-schema-evolution/v1', 'state': 'evolved_not_reconciled',
        'board_id': board_id, 'snapshot_sha256': snapshot.manifest_sha256, 'builds': asdict(builds),
        'source_database_uuid': graph['database_uuid'], 'native_history': manifest['native_graphs'][index],
        'before': graph['certificate'], 'after': asdict(report),
        'introduced_properties': sorted(_INTRODUCED), 'introduced_relation_layouts': 11,
        'history_access': 'retained_predecessor_native_backup', 'runtime_admission': 'not_authorized'}
