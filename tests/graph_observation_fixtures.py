"""Native setup for disposable preexisting observations, never a product API."""

from contextlib import contextmanager

from okto_grafx import TextIndexOptions
from okto_pulse.community.adapters.grafx_observations import scope


def enable_fixture_history(database, *, relationships=()):
    tables, _ = scope(("Decision",), relationships)
    database.ensure_identity_indexes()
    database.enable_commit_history()
    database.enable_system_history(tables)


def prepare_fixture_index(database):
    if not any(i.name == "pulse_text_v1_Decision" for i in database.indexes.indexes()):
        database.create_text_index(
            "pulse_text_v1_Decision", "Decision", ("title", "content"),
            options=TextIndexOptions(field_weights=(2.0, 1.0), statistics_mode="durable"),
        )


@contextmanager
def routed_fixture_database(bundle, board_id):
    """Caller holds the disposable board's writer guard; retire cached readers."""
    snapshot = bundle.resolver.acquire_board_route(board_id)
    for pool in bundle.board.grafx_read_pools:
        pool.close_all()
    bundle.grafx_pool.close(snapshot.active_path)
    try:
        database = bundle.grafx_pool.get(snapshot.active_path, page_size=snapshot.page_size)
        bundle.resolver.admit_grafx_route(snapshot, database, operation="test_fixture")
        yield database
        bundle.resolver.revalidate_snapshot(snapshot, require_physical=True)
    finally:
        bundle.grafx_pool.close(snapshot.active_path)
