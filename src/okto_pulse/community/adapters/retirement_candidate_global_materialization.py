"""Create only an absent Global inside an unpublished, offline retirement stage.

Existing history is never replaced here. The enclosing coordinator owns cleanup
on failure, the source fence, provenance, and the eventual candidate checkpoint.
"""

from okto_grafx import connect
from okto_pulse.core.kg.interfaces.graph_errors import GraphCapabilityUnavailable
from okto_pulse.core.kg.logical_transfer import LogicalTimestamp
from okto_pulse.core.ports.global_projection import compare_global_projection

from .graph_backend_binding import CommunityGraphBackendBindingStore
from .grafx_global_discovery_runtime import CommunityGrafxGlobalDiscoveryRuntime
from .grafx_global_recovery_batch import RECOVERY_DIGEST_BATCH_SIZE
from .logical_transfer_schema import global_logical_schema
from .relational_recovery_snapshot import _check_time, _deadline
from .retirement_candidate_global_reconciliation import derive_candidate_global_seeds, _read
from .joint_recovery_snapshot import _explicit_path

_TIMESTAMP = '1970-01-01T00:00:00Z'


def materialize_missing_candidate_global(target, source_inputs, *, settings, generation,
        require_live, max_seconds):
    """No public entrypoint, original mutation, fallback layer, or cutover grant."""
    deadline = _deadline(max_seconds)

    def fence(phase):
        _check_time(deadline)
        if require_live() is not True:
            raise ValueError('retirement_global_execution_fence_lost')

    fence('prepare')
    bindings = CommunityGraphBackendBindingStore(target / 'kg-artifacts')
    try:
        bindings.inspect_global_binding()
    except GraphCapabilityUnavailable as error:
        if error.details.get('reason') != 'binding_missing':
            raise
    else:
        return {'state': 'retained'}
    if source_inputs['state'] == 'overlay_unavailable':
        return {'state': 'unavailable'}
    seeds = derive_candidate_global_seeds(target, source_inputs, settings=settings, deadline=deadline)
    if not seeds:
        return {'state': 'not_applicable'}
    # Preserve the existing recovery writer's admitted layers. Do not infer one.
    if any(digest.graph_layer not in {'canonical', 'working'} for seed in seeds for digest in seed.digests):
        return {'state': 'unsupported_source_layer'}
    expected = compare_global_projection(schema=global_logical_schema(), nodes=(), relations=(), seeds=seeds)
    path = _explicit_path(bindings.global_grafx_path(generation))
    if path.exists():
        raise ValueError('retirement_global_unbound_artifact_present')
    fence('create')
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path, page_size=settings.kg_grafx_page_size) as database:
        runtime = CommunityGrafxGlobalDiscoveryRuntime(lambda: database, lambda: path, database.close, fence)
        runtime.ensure_layer_schema()
        for seed in seeds:
            runtime.upsert_board_summary(board_id=seed.board_id, name=seed.board_name or seed.board_id,
                summary=seed.summary, summary_embedding=list(seed.summary_embedding),
                decision_count=len(seed.digests), synced_at=_TIMESTAMP)
            for offset in range(0, len(seed.digests), RECOVERY_DIGEST_BATCH_SIZE):
                rows = tuple({'digest_id': f'dd_{seed.board_id[:8]}_{digest.original_node_id}',
                    'board_id': seed.board_id, 'original_node_id': digest.original_node_id,
                    'title': digest.title, 'summary': digest.summary, 'node_type': digest.node_type,
                    'graph_layer': digest.graph_layer, 'embedding': list(digest.embedding),
                    'created_at': _TIMESTAMP} for digest in seed.digests[offset:offset + RECOVERY_DIGEST_BATCH_SIZE])
                runtime.create_recovery_digest_batch(board_id=seed.board_id, rows=rows)
        fence('checkpoint')
        database.checkpoint()
        fence('binding')
        bound = bindings.initialize_global_binding(backend='grafx', generation=generation, physical_path=path,
            page_size=settings.kg_grafx_page_size, database=database)
    schema, nodes, relations = _read(bound, 'global_discovery', deadline)
    observed = compare_global_projection(schema=schema, nodes=nodes, relations=relations, seeds=seeds)
    if observed.state != 'matched' or observed.expected_sha256 != expected.expected_sha256:
        raise ValueError('retirement_global_materialization_mismatch')
    fence('completed')
    return {'state': 'created', 'generation': generation, 'expected_sha256': observed.expected_sha256}


def verify_candidate_global_materialization(target, source_inputs, receipt, *, settings,
        generation, had_global, comparison, max_seconds):
    """Re-derive creation authority from retained source history, never its flag."""
    deadline = _deadline(max_seconds)
    if had_global:
        expected = {'state': 'retained'}
    elif source_inputs['state'] == 'overlay_unavailable':
        expected = {'state': 'unavailable'}
    else:
        seeds = derive_candidate_global_seeds(target, source_inputs, settings=settings, deadline=deadline)
        if not seeds:
            expected = {'state': 'not_applicable'}
        elif any(digest.graph_layer not in {'canonical', 'working'} for seed in seeds for digest in seed.digests):
            expected = {'state': 'unsupported_source_layer'}
        else:
            if comparison['state'] != 'matched' or not comparison['materialized']:
                raise ValueError('retirement_global_materialization_mismatch')
            binding = CommunityGraphBackendBindingStore(target / 'kg-artifacts').inspect_global_binding()
            if binding.generation != generation:
                raise ValueError('retirement_global_materialization_generation_changed')
            _, nodes, _ = _read(binding, 'global_discovery', deadline)
            for node in nodes:
                field = 'last_sync_at' if node.type_name == 'Board' else 'created_at'
                if node.properties.get(field) != LogicalTimestamp(0):
                    raise ValueError('retirement_global_materialization_timestamp_changed')
            expected = {'state': 'created', 'generation': generation, 'expected_sha256': comparison['expected_sha256']}
    if receipt != expected:
        raise ValueError('retirement_global_materialization_receipt_changed')
