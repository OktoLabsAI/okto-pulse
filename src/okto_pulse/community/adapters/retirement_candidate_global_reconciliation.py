"""Cold Global comparison using authenticated candidate source inputs and Core policy."""

from dataclasses import asdict
import json

from okto_grafx import connect

from okto_pulse.core.kg.interfaces.graph_errors import GraphCapabilityUnavailable
from okto_pulse.core.kg.logical_transfer import canonical_bytes, encode_value
from okto_pulse.core.ports.global_discovery_recovery_control import GlobalDiscoveryRecoveryBoardSeedInput
from okto_pulse.core.ports.global_projection import (
    build_global_projection_seed, compare_global_projection, global_projection_sources_from_inventory,
    global_projection_summary_text,
)

from .composition import build_community_embedding
from .graph_backend_binding import CommunityGraphBackendBindingStore
from .logical_transfer_factories import make_grafx_logical_source
from .logical_transfer_schema import global_logical_schema
from .relational_recovery_snapshot import _check_time, _deadline


def _read(binding, scope, deadline):
    if binding.backend != 'grafx':
        raise ValueError('retirement_global_backend_invalid')
    with connect(binding.physical_path, page_size=binding.page_size, read_only=True) as database:
        reader = make_grafx_logical_source(database, scope=scope).open_snapshot()
        try:
            schema, counts = reader.schema(), reader.counts()
            if counts.nodes > 100_000 or counts.relations > 500_000:
                raise ValueError('retirement_global_inventory_limit')
            nodes, relations, budget = [], [], 0
            for batches, records in ((reader.iter_nodes(batch_size=500), nodes),
                    (reader.iter_relations(batch_size=500), relations)):
                for batch in batches:
                    _check_time(deadline)
                    for record in batch:
                        identity = ((record.type_name, record.key) if records is nodes else
                            (record.layout_name, record.source_type, record.source_key, record.target_type, record.target_key))
                        budget += len(canonical_bytes({'identity': identity,
                            'properties': {key: encode_value(value) for key, value in record.properties.items()}}))
                        if budget > 64 * 1024 * 1024:
                            raise ValueError('retirement_global_inventory_limit')
                        records.append(record)
            if len(nodes) != counts.nodes or len(relations) != counts.relations:
                raise ValueError('retirement_global_inventory_changed')
            return schema, tuple(nodes), tuple(relations)
        finally:
            reader.close()


def compare_candidate_global_projection(target, source_inputs, *, settings, max_seconds):
    """Called only under the coordinator's source/candidate offline fences."""
    if source_inputs['state'] == 'overlay_unavailable':
        return {'state': 'unavailable', 'reason': source_inputs['reason']}
    if source_inputs['state'] not in {'captured_not_reconciled', 'not_applicable'}:
        raise ValueError('retirement_global_source_inputs_invalid')
    deadline = _deadline(max_seconds)
    bindings, seeds, budget = CommunityGraphBackendBindingStore(target / 'kg-artifacts'), [], 0
    provider = build_community_embedding(settings=settings)
    for payload in source_inputs['boards']:
        source_input = GlobalDiscoveryRecoveryBoardSeedInput(**payload)
        schema, nodes, relations = _read(bindings.inspect_board_binding(source_input.board_id), 'board', deadline)
        sources = global_projection_sources_from_inventory(schema=schema, nodes=nodes, relations=relations)
        summary = tuple(float(value) for value in provider.encode(global_projection_summary_text(
            board_id=source_input.board_id, board_name=source_input.board_name)))
        _check_time(deadline)
        seed = build_global_projection_seed(source_input=source_input,
            expected_sources=tuple((source.node_id, source.node_type) for source in sources),
            sources=sources, summary_embedding=summary)
        budget += len(json.dumps(seed.to_dict(), ensure_ascii=False, allow_nan=False).encode('utf-8'))
        if budget > 64 * 1024 * 1024:
            raise ValueError('retirement_global_seed_limit')
        seeds.append(seed)
    try:
        binding = bindings.inspect_global_binding()
    except GraphCapabilityUnavailable as error:
        if error.details.get('reason') != 'binding_missing':
            raise
        schema, nodes, relations = global_logical_schema(), (), ()
        materialized = False
    else:
        schema, nodes, relations = _read(binding, 'global_discovery', deadline)
        materialized = True
    comparison = compare_global_projection(schema=schema, nodes=nodes, relations=relations, seeds=tuple(seeds))
    _check_time(deadline)
    return {**asdict(comparison), 'materialized': materialized,
        'board_source_hashes': {seed.board_id: seed.source_inventory_hash for seed in seeds}}
