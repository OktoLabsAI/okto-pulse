"""Private deterministic execution during the retained candidate restore window.

This stage has no public entrypoint and does not certify reconciliation/cutover.
It retains exact ACKs and the complete resulting SQL digest for later verification.
"""

import hashlib

from okto_pulse.core import configure_settings, configure_storage
from okto_pulse.core.composition import isolated_runtime_provider_scope
from okto_pulse.core.kg.interfaces.graph_errors import GraphCapabilityUnavailable
from okto_pulse.core.ports.consolidation import ConsolidationClaimScope
from okto_pulse.core.ports.offline_kg_recovery import issue_offline_recovery_capability, reserve_offline_consolidation
from okto_pulse.core.services.application_kg import guarded_board_write, drain_kg_health_probes

from .board_rebuild_ingestion import CommunityBoardRebuildIngestionAdapter
from .composition import configure_community_kg_registry, require_community_routed_graph_composition
from .coordination import register_community_coordination_providers, build_root_bound_community_write_lock_port
from okto_pulse.core.ports.coordination import register_coordination_providers
from .relational_effects import register_community_relational_effects
from .relational_recovery_snapshot import _deadline, _check_time
from .retirement_bootstrap import _snapshot
from .retirement_projection_inputs import revalidate_retirement_projection_inputs
from .sqlalchemy_database import configure_community_database
from .storage import CommunityFileSystemStorage
from .sprint_retirement_archive import _encode


def projection_execution_binding(*, board_id, item, seed, seed_document, projection, settings):
    """The retained plan and candidate generation own one exact run identity."""
    settings_digest = hashlib.sha256(_encode(settings.model_dump(mode='json'))).hexdigest()
    binding = {'format': 'retirement-projection-execution/v1', 'board_id': board_id,
        'seed_sha256': seed.manifest_sha256, 'offline_run_sha256': projection['offline_run_sha256'],
        'projection_sha256': item['sha256'], 'generation': seed_document['generation'],
        'migration_builds': projection['migration_builds'], 'original_backup': projection['original_backup'],
        'candidate_snapshot': seed_document['snapshot'], 'execution_settings_sha256': settings_digest}
    return binding, hashlib.sha256(_encode(binding)).hexdigest()


async def execute_candidate_projection(stage, *, seed, seed_document, projection, settings, lifetime_probe, max_seconds):
    """Only the restore coordinator supplies this still-private, fenced stage."""
    deadline = _deadline(max_seconds)
    source, kg = stage / 'database.sqlite3', stage / 'kg-artifacts'
    candidate_settings = settings.model_copy(update={'database_url': f'sqlite+aiosqlite:///{source}',
        'data_dir': str(stage), 'kg_base_dir': str(kg), 'upload_dir': str(stage / 'uploads')})
    def require_live():
        _check_time(deadline)
        if lifetime_probe() is not True:
            raise RuntimeError('retirement_candidate_execution_authority_lost')
    require_live()
    with isolated_runtime_provider_scope(inherit=False):
        configure_settings(candidate_settings)
        configure_storage(CommunityFileSystemStorage(str(stage / 'uploads')))
        runtime = configure_community_database(candidate_settings.database_url)
        bundle = None
        try:
            register_community_coordination_providers()
            write_port = build_root_bound_community_write_lock_port(kg)
            register_coordination_providers(write_lock_port=write_port)
            register_community_relational_effects(settings=candidate_settings)
            configure_community_kg_registry(runtime.session_factory, settings=candidate_settings)
            bundle = require_community_routed_graph_composition()
            async with runtime.engine.connect() as connection:
                await connection.exec_driver_sql('BEGIN IMMEDIATE')
                try:
                    baseline = await connection.run_sync(_snapshot)
                    membership = await revalidate_retirement_projection_inputs(connection, source, projection,
                        max_seconds=max_seconds)
                finally:
                    await connection.rollback()
            boards, accumulated = [], 0
            for item in projection['boards']:
                require_live()
                board_id = item['projection']['board_id']
                sources = membership[board_id]
                binding, lineage = projection_execution_binding(board_id=board_id, item=item, seed=seed,
                    seed_document=seed_document, projection=projection, settings=settings)
                run_id = 'retirement-' + lineage
                scope = ConsolidationClaimScope(board_id=board_id, source='rebuild:' + run_id,
                    reservation_lineage_id=lineage)
                receipts = []
                if sources:
                    with issue_offline_recovery_capability(board_id=board_id, lifetime_probe=lifetime_probe) as capability:
                        with reserve_offline_consolidation(claim_scope=scope, recovery_capability=capability,
                                write_lock_port=write_port, relational_scope_factory=runtime.session_factory,
                                owner_id='retirement-installer') as reserved:
                            with guarded_board_write(board_id, operation='retirement_candidate_prepare',
                                    owner_id='retirement-installer', mutation_ref=lineage) as writer:
                                try:
                                    current = bundle.binding_store.inspect_board_binding(board_id)
                                except GraphCapabilityUnavailable as failure:
                                    if failure.details.get('reason') != 'binding_missing':
                                        raise
                                    current = None
                                if current is None:
                                    path = bundle.binding_store.board_grafx_path(board_id, seed_document['generation'])
                                    path.parent.mkdir(parents=True, exist_ok=True)
                                    graph = bundle.grafx_pool.get(path, page_size=candidate_settings.kg_grafx_page_size)
                                    bundle.binding_store.initialize_board_binding(board_id=board_id, backend='grafx',
                                        generation=seed_document['generation'], physical_path=path,
                                        page_size=candidate_settings.kg_grafx_page_size, database=graph)
                                elif current.generation != seed_document['generation']:
                                    raise ValueError('retirement_candidate_execution_generation_mismatch')
                                await bundle.graph_schema_manager.ensure_bootstrapped(board_id)
                                require_live()
                                CommunityBoardRebuildIngestionAdapter(db_path=source).enqueue_sources(
                                    board_id=board_id, run_id=run_id, sources=sources,
                                    mutation_guard=reserved.is_authorized)
                                writer.ensure_durable()
                            expected = {(row['source_ref'], row['source_version'], row['content_hash']) for row in sources}
                            observed = set()
                            for _ in sources:
                                require_live()
                                batch = await reserved.process_next()
                                if batch.acked_count != 1 or len(batch.rows) != 1 or batch.rows[0].ack_receipt is None:
                                    raise RuntimeError('retirement_candidate_execution_not_acked')
                                ack = batch.rows[0].ack_receipt
                                identity = ack.membership_source_ref, ack.membership_source_version, ack.membership_content_hash
                                if identity not in expected or identity in observed:
                                    raise RuntimeError('retirement_candidate_execution_ack_membership_mismatch')
                                observed.add(identity)
                                receipt = ack.to_payload()
                                accumulated += len(_encode(receipt))
                                if accumulated > 64 * 1024 * 1024:
                                    raise ValueError('retirement_candidate_execution_receipt_limit')
                                receipts.append(receipt)
                            if observed != expected:
                                raise RuntimeError('retirement_candidate_execution_ack_incomplete')
                boards.append({'binding': binding, 'reservation_lineage_id': lineage, 'acks': receipts})
            require_live()
            async with runtime.engine.connect() as connection:
                await connection.exec_driver_sql('BEGIN IMMEDIATE')
                try:
                    await revalidate_retirement_projection_inputs(connection, source, projection, max_seconds=max_seconds)
                    final = await connection.run_sync(_snapshot)
                finally:
                    await connection.rollback()
            return {'format': 'retirement-candidate-projection/v1', 'seed_sha256': seed.manifest_sha256,
                'state': 'projected_not_reconciled', 'before_sql': baseline, 'after_sql': final, 'boards': boards}
        finally:
            if bundle is not None:
                if drain_kg_health_probes() != 0:
                    raise RuntimeError('retirement_candidate_graph_jobs_still_running')
                bundle.grafx_pool.close_all()
                for pool in bundle.board.grafx_read_pools:
                    pool.close_all()
            await runtime.close()
