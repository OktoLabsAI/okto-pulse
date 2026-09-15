"""No latency gates: event barriers prove overlap, drain and native isolation."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from okto_pulse.community.adapters.global_operation_gate import GlobalOperationGate
from okto_pulse.core.kg.interfaces.graph_errors import GraphUnavailable


def test_readers_overlap_writer_but_lifecycle_drains_and_reentry_works():
    gate = GlobalOperationGate(timeout=2)
    active, release, reader, admin = Event(), Event(), Event(), Event()

    def write():
        with gate.operation(write=True):
            active.set()
            assert release.wait(2)
            with gate.operation(write=True), gate:
                pass

    def read():
        with gate.operation():
            reader.set()

    def close():
        with gate:
            admin.set()

    with ThreadPoolExecutor(3) as executor:
        writer = executor.submit(write)
        assert active.wait(2)
        executor.submit(read).result(2)
        assert reader.is_set()
        closing = executor.submit(close)
        assert not admin.wait(0.03)
        release.set()
        writer.result(2)
        closing.result(2)
    assert not gate._pins and gate._owner is None


def test_timed_out_drain_does_not_close_or_leak_admission():
    gate = GlobalOperationGate(timeout=0.03)
    with gate.operation():
        with ThreadPoolExecutor(1) as executor:

            def close():
                with gate:
                    pytest.fail("cannot drain a live foreign pin")

            with pytest.raises(GraphUnavailable):
                executor.submit(close).result(2)
        with gate.operation():
            pass
    with gate:
        pass
    assert not gate._pins and gate._waiting == 0


def test_native_reader_completes_inside_writer_verification_and_shutdown_closes_all(
    tmp_path,
):
    from okto_pulse.community.adapters.graph_backend_binding import (
        CommunityGraphBackendBindingStore,
    )
    from okto_pulse.community.adapters.graph_route_resolver import (
        CommunityGraphRouteResolver,
    )
    from okto_pulse.community.adapters.grafx_database_pool import (
        CommunityGrafxDatabasePool,
    )
    from okto_pulse.community.adapters.routed_global_graph_composition import (
        build_community_routed_global_graph_composition,
    )
    from okto_pulse.community.config import PULSE_GRAFX_DEFAULT_PAGE_SIZE

    store = CommunityGraphBackendBindingStore(tmp_path)
    resolver = CommunityGraphRouteResolver(
        store,
        board_backend="grafx",
        global_backend="grafx",
        grafx_page_size=PULSE_GRAFX_DEFAULT_PAGE_SIZE,
    )
    bundle = build_community_routed_global_graph_composition(
        binding_store=store,
        resolver=resolver,
        grafx_pool=CommunityGrafxDatabasePool(tmp_path),
        global_lock=GlobalOperationGate(),
        read_participants=2,
        revalidate_write_fence=lambda _: None,
    )
    bundle.initialize_global_route()
    try:
        with bundle.runtime.post_write_verification_scope():
            bundle.runtime.upsert_board_summary(
                board_id="test",
                name="Test",
                summary="visible",
                summary_embedding=[1.0] + [0.0] * 383,
                decision_count=0,
                synced_at="2026-09-13T00:00:00Z",
            )
            with ThreadPoolExecutor(2) as executor:
                result = executor.submit(
                    bundle.runtime.execute, "MATCH (n:Board) RETURN n.board_id"
                ).result(15)
                assert result.rows == (("test",),)
            bundle.runtime.flush_after_write_batch()
        assert bundle.runtime.list_schema_objects()
        from okto_pulse.core.kg.interfaces.graph_errors import (
            GraphCapabilityUnavailable,
        )

        snapshot = resolver.acquire_global_route()
        with bundle.runtime._read_factory(snapshot) as session:
            with pytest.raises(GraphCapabilityUnavailable):
                session.runtime.execute("CREATE (:Board {board_id: 'forbidden'})")
        assert bundle.runtime.execute("MATCH (n:Board) RETURN n.board_id").rows == (
            ("test",),
        )
    finally:
        bundle.shutdown()
    assert not bundle.shutdown._grafx.tracked_paths
    assert all(not manager.tracked_paths for manager in bundle.shutdown._grafx.readers)
