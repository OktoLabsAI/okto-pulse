"""KG-19: real process loss after graph commit and before relational ACK."""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from okto_pulse.core import configure_settings
from okto_pulse.core.application.processors.consolidation import ConsolidationProcessor
from okto_pulse.core.ports.coordination import register_coordination_providers
from okto_pulse.core.services.application_kg import drain_kg_health_probes
from okto_pulse.community.config import CommunitySettings
from okto_pulse.community.adapters import sqlalchemy_database as db
from okto_pulse.community.adapters.composition import configure_community_kg_registry, require_community_routed_graph_composition
from okto_pulse.community.adapters.coordination import register_community_coordination_providers, build_root_bound_community_write_lock_port
from okto_pulse.community.adapters.relational_effects import register_community_relational_effects
from okto_pulse.community.adapters.graph_backend_binding import CommunityGraphBackendBindingStore
from okto_pulse.community.adapters.sqlalchemy_models import ConsolidationAudit, ConsolidationQueue, Spec

from test_projection_materialized_parity import materialize, relationship_set, source


@asynccontextmanager
async def reopen_native(root):
    """Open the existing native source and routed graph; do not seed or rebuild."""
    settings = CommunitySettings(database_url=f"sqlite+aiosqlite:///{root / 'source.sqlite3'}",
        data_dir=str(root), kg_base_dir=str(root / "kg"), kg_embedding_mode="stub", kg_embedding_dim=384)
    configure_settings(settings)
    runtime = db.configure_community_database(settings.database_url)
    register_community_coordination_providers()
    register_coordination_providers(write_lock_port=build_root_bound_community_write_lock_port(root / "kg"))
    register_community_relational_effects(settings=settings)
    configure_community_kg_registry(runtime.session_factory, settings=settings)
    bundle = require_community_routed_graph_composition()
    physical = CommunityGraphBackendBindingStore(root / "kg").board_grafx_path("board", "parity")
    try:
        yield runtime.session_factory, bundle.grafx_pool.get(physical, page_size=8192)
    finally:
        drain_kg_health_probes()
        bundle.global_graph.close_all_on_shutdown()
        bundle.grafx_pool.close_all()
        for pool in (*bundle.board.grafx_read_pools, *bundle.board.grafx_query_pools):
            pool.close_all()
        await runtime.close()


CHILD = r"""
import asyncio
import os
from pathlib import Path
import sys
from okto_pulse.core.application.processors import consolidation
from okto_pulse.core.kg.guarded_write import GuardedWriteLease
from test_native_projection_crash_replay import reopen_native

root = Path(sys.argv[1])
original_ensure = GuardedWriteLease.ensure_owned
original_write = consolidation.guarded_board_write

def short_test_lease(*args, **kwargs):
    kwargs["ttl_seconds"] = 5
    return original_write(*args, **kwargs)

def kill_before_ack(self, *, failure_phase="ownership_check"):
    original_ensure(self, failure_phase=failure_phase)
    if failure_phase == "before_relational_ack":
        assert self.durability_applied
        (root / "crash-cut.txt").write_text("graph committed; SQL ACK not committed", encoding="utf-8")
        os._exit(73)

async def main():
    GuardedWriteLease.ensure_owned = kill_before_ack
    consolidation.guarded_board_write = short_test_lease
    async with reopen_native(root) as (factory, _graph):
        await consolidation.ConsolidationProcessor(relational_scope_factory=factory).process_batch()
    raise AssertionError("worker did not reach the requested crash boundary")

asyncio.run(main())
"""


@pytest.mark.asyncio
@pytest.mark.timeout(300)
async def test_real_crash_before_ack_replays_current_source_without_duplicate_relations(tmp_path):
    root = tmp_path / "crash-replay"
    initial_audits = []

    async def prepare(factory, _graph):
        async with factory() as session:
            initial_audits.extend(await session.scalars(select(ConsolidationAudit.session_id)))
            spec = await session.get(Spec, "spec")
            for field, value in source(["ac_one"]).items():
                setattr(spec, field, value)
            spec.version = 2
            session.add(ConsolidationQueue(id="crashed-projection", board_id="board",
                artifact_type="spec", artifact_id="spec", source="state_transition"))
            await session.commit()

    await materialize(root, incremental=False, exercise=prepare, native_schema=True)
    child = await asyncio.to_thread(subprocess.run,
        [sys.executable, "-X", "utf8", "-c", CHILD, str(root)],
        cwd=Path(__file__).parent, env=dict(os.environ), capture_output=True, text=True,
        encoding="utf-8", timeout=120)
    assert child.returncode == 73, child.stdout + child.stderr
    assert (root / "crash-cut.txt").read_text(encoding="utf-8") == "graph committed; SQL ACK not committed"
    # Let the actual five-second writer lease expire; never erase its marker.
    await asyncio.sleep(6)

    async with reopen_native(root) as (factory, graph):
        crashed = relationship_set(graph)
        tests = {edge[2] for edge in crashed if edge[3] == "tests/ac_match@v2.1"}
        assert tests == {"spec:spec:ac:ac_one"}
        async with factory() as session:
            pending = await session.get(ConsolidationQueue, "crashed-projection")
            assert pending is not None and pending.status == "claimed"
            assert pending.claim_token and pending.claim_timeout_at is not None
            expires = pending.claim_timeout_at
            assert list(await session.scalars(select(ConsolidationAudit.session_id))) == initial_audits
            # A newer authoritative revision arrives while the old claim remains.
            spec = await session.get(Spec, "spec")
            for field, value in source(["ac_one", "ac_two"]).items():
                setattr(spec, field, value)
            spec.version = 3
            await session.commit()

        recovery = ConsolidationProcessor(relational_scope_factory=factory,
            clock=SimpleNamespace(now=lambda: expires + timedelta(seconds=1)))
        assert await recovery.recover_stale_claims() == 1
        processor = ConsolidationProcessor(relational_scope_factory=factory)
        assert await processor.process_batch() == 1
        current = relationship_set(graph)
        assert {edge[2] for edge in current if edge[3] == "tests/ac_match@v2.1"} == {
            "spec:spec:ac:ac_one", "spec:spec:ac:ac_two"}
        assert all(count == 1 for count in current.values())
        async with factory() as session:
            assert not list(await session.scalars(select(ConsolidationQueue.id)))
            assert (await session.get(Spec, "spec")).version == 3
            session.add(ConsolidationQueue(id="old-trigger-replay", board_id="board",
                artifact_type="spec", artifact_id="spec", source="state_transition"))
            await session.commit()
        assert await processor.process_batch() == 1
        assert relationship_set(graph) == current
