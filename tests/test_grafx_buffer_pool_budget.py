"""Community's optional per-handle Grafx buffer budget and factory compatibility."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from okto_pulse.community.adapters.grafx_database_pool import (
    CommunityGrafxDatabasePool,
    GrafxDatabasePoolError,
)
from okto_pulse.community.adapters.routed_board_graph_composition import (
    build_community_routed_board_graph_composition,
)
from okto_pulse.community.adapters.routed_graph_composition import (
    build_community_routed_graph_composition,
)
from okto_pulse.community.config import CommunitySettings


MIB = 1024 * 1024
PAGE_SIZE = 8192


def _database(path, *, page_size, descriptor_revalidation):
    return SimpleNamespace(
        path=str(path),
        identity=SimpleNamespace(page_size=page_size),
        descriptor_revalidation=descriptor_revalidation,
        close=lambda: None,
    )


def _settings(root, budget=64):
    return SimpleNamespace(
        kg_base_dir=str(root),
        kg_graph_backend="grafx",
        kg_global_graph_backend="grafx",
        kg_grafx_page_size=PAGE_SIZE,
        kg_grafx_descriptor_revalidation="strict",
        kg_grafx_buffer_pool_mb=budget,
        kg_ladybug_max_db_size_gb=2,
    )


def test_settings_keep_default_and_accept_environment_override(monkeypatch):
    monkeypatch.delenv("KG_GRAFX_BUFFER_POOL_MB", raising=False)
    assert CommunitySettings(_env_file=None).kg_grafx_buffer_pool_mb == 64
    monkeypatch.setenv("KG_GRAFX_BUFFER_POOL_MB", "128")
    assert CommunitySettings(_env_file=None).kg_grafx_buffer_pool_mb == 128
    assert (
        CommunitySettings(
            _env_file=None, kg_grafx_buffer_pool_mb=256
        ).kg_grafx_buffer_pool_mb
        == 256
    )


@pytest.mark.parametrize("value", [0, -1, True, 128.5, "128.5", "invalid"])
def test_settings_reject_invalid_buffer_budget(value):
    with pytest.raises(ValidationError, match="positive integer"):
        CommunitySettings(_env_file=None, kg_grafx_buffer_pool_mb=value)


@pytest.mark.parametrize("value", [0, -1, True, 128.0, "128"])
def test_pool_rejects_invalid_budget_before_open(tmp_path, value):
    with pytest.raises(GrafxDatabasePoolError) as caught:
        CommunityGrafxDatabasePool(tmp_path, buffer_pool_mb=value)
    assert caught.value.reason == "pool_buffer_pool_mb_invalid"


def test_default_preserves_narrow_custom_factory_signature(tmp_path):
    calls = []

    def legacy_factory(path, *, page_size, descriptor_revalidation):
        calls.append(path)
        return _database(
            path, page_size=page_size, descriptor_revalidation=descriptor_revalidation
        )

    pool = CommunityGrafxDatabasePool(tmp_path, connect=legacy_factory)
    path = tmp_path / "legacy"
    first = pool.get(path, page_size=PAGE_SIZE)
    assert pool.get(path, page_size=PAGE_SIZE) is first
    assert calls == [path]
    assert pool.buffer_pool_mb == 64
    pool.close_all()


def test_nondefault_budget_is_not_silently_dropped_for_narrow_factory(tmp_path):
    pool = CommunityGrafxDatabasePool(tmp_path, connect=_database, buffer_pool_mb=128)
    with pytest.raises(GrafxDatabasePoolError) as caught:
        pool.get(tmp_path / "unsupported", page_size=PAGE_SIZE)
    assert caught.value.reason == "pool_open_failed"
    assert isinstance(caught.value.__cause__, TypeError)
    assert len(pool) == 0


@pytest.mark.parametrize("budget", [128, 256])
def test_budget_reaches_writer_both_read_lanes_and_unpooled_recovery(tmp_path, budget):
    calls = []

    def connector(path, **options):
        calls.append(dict(options))
        return _database(
            path,
            page_size=options["page_size"],
            descriptor_revalidation=options["descriptor_revalidation"],
        )

    bundle = build_community_routed_board_graph_composition(
        settings=_settings(tmp_path, budget),
        grafx_connect=connector,
    )
    assert calls == []  # Composition does not open any database.
    path = tmp_path / "budget-lanes"
    pools = (bundle.grafx_pool, *bundle.grafx_read_pools)
    assert len(pools) == 3
    for pool in pools:
        database = pool.get(path, page_size=PAGE_SIZE)
        assert pool.get(path, page_size=PAGE_SIZE) is database
        assert pool.buffer_pool_mb == budget
    assert calls == [
        {
            "page_size": PAGE_SIZE,
            "descriptor_revalidation": "strict",
            "buffer_budget_bytes": budget * MIB,
        },
        {
            "page_size": PAGE_SIZE,
            "descriptor_revalidation": "strict",
            "buffer_budget_bytes": budget * MIB,
            "read_only": True,
        },
        {
            "page_size": PAGE_SIZE,
            "descriptor_revalidation": "strict",
            "buffer_budget_bytes": budget * MIB,
            "read_only": True,
        },
    ]
    temporary = bundle.grafx_pool.open_unpooled(
        path, page_size=PAGE_SIZE, connect=connector
    )
    assert calls[-1]["buffer_budget_bytes"] == budget * MIB
    temporary.close()
    for pool in pools:
        pool.close_all()


def test_shared_pool_reuse_requires_same_budget(tmp_path):
    first = build_community_routed_board_graph_composition(
        settings=_settings(tmp_path, 128)
    )
    shared = dict(
        binding_store=first.binding_store,
        resolver=first.resolver,
        grafx_pool=first.grafx_pool,
    )
    second = build_community_routed_board_graph_composition(
        settings=_settings(tmp_path, 128),
        **shared,
    )
    assert second.grafx_pool is first.grafx_pool
    assert all(pool.buffer_pool_mb == 128 for pool in second.grafx_read_pools)
    with pytest.raises(ValueError, match="buffer budget must match settings"):
        build_community_routed_board_graph_composition(
            settings=_settings(tmp_path, 256),
            **shared,
        )


@pytest.mark.parametrize("budget", [128, 256])
def test_complete_composition_shares_nondefault_budget_with_global_pool(
    tmp_path, budget
):
    calls = []

    def connector(path, **options):
        calls.append(dict(options))
        return _database(
            path,
            page_size=options["page_size"],
            descriptor_revalidation=options["descriptor_revalidation"],
        )

    bundle = build_community_routed_graph_composition(
        settings=_settings(tmp_path, budget),
        grafx_connect=connector,
    )
    assert calls == []
    assert (
        bundle.global_graph.grafx_pool is bundle.board.grafx_pool is bundle.grafx_pool
    )
    assert bundle.global_graph.grafx_pool.buffer_pool_mb == budget
    assert all(pool.buffer_pool_mb == budget for pool in bundle.board.grafx_read_pools)
    path = tmp_path / "global-budget"
    opened = bundle.global_graph.grafx_pool.get(path, page_size=PAGE_SIZE)
    assert bundle.board.grafx_pool.get(path, page_size=PAGE_SIZE) is opened
    assert calls == [
        {
            "page_size": PAGE_SIZE,
            "descriptor_revalidation": "strict",
            "buffer_budget_bytes": budget * MIB,
        }
    ]
    bundle.grafx_pool.close_all()


@pytest.mark.parametrize("budget", [64, 128, 256])
def test_native_handles_report_effective_budget_and_share_durable_rows(
    tmp_path: Path, budget
):
    path = tmp_path / "native-budget"
    writer_pool = CommunityGrafxDatabasePool(tmp_path, buffer_pool_mb=budget)
    reader_pool = CommunityGrafxDatabasePool(
        tmp_path, buffer_pool_mb=budget, read_only=True
    )
    try:
        writer = writer_pool.get(path, page_size=PAGE_SIZE)
        with writer.begin("write") as transaction:
            transaction.execute("CREATE NODE TABLE Item(id STRING, PRIMARY KEY(id))")
            transaction.execute("CREATE (:Item {id: 'budget-proof'})")
        writer.checkpoint()
        reader = reader_pool.get(path, page_size=PAGE_SIZE)
        assert writer.pool.budget_bytes == budget * MIB
        assert reader.pool.budget_bytes == budget * MIB
        assert writer.pool.capacity_pages == budget * MIB // PAGE_SIZE
        assert reader.execute("MATCH (n:Item) RETURN n.id").rows == (("budget-proof",),)
    finally:
        reader_pool.close_all()
        writer_pool.close_all()
