"""Settings inventory, validation, persistence and actual constructor forwarding."""

from dataclasses import fields
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_grafx.runtime.config import DatabaseConfig
from okto_pulse.community.adapters import sqlalchemy_runtime_settings_service as service
from okto_pulse.community.adapters.grafx_database_pool import CommunityGrafxDatabasePool
from okto_pulse.community.adapters.grafx_settings_catalog import (
    ALIASES,
    DEFAULTS,
    EDITABLE,
    HELP,
    MANAGED,
    settings_catalog,
    validate_options,
)
from okto_pulse.community.adapters.sqlalchemy_models import AppSetting
from okto_pulse.community.api.settings import (
    RuntimeSettingsPayload,
    RuntimeSettingsResponse,
)
from okto_pulse.community.config import CommunitySettings


def test_every_native_config_option_has_one_reviewed_ui_policy_and_help():
    native = {field.name for field in fields(DatabaseConfig)}
    assert native == set(HELP) == {row["name"] for row in settings_catalog()}
    assert native == EDITABLE | MANAGED | ALIASES.keys()
    assert len(settings_catalog()) == len(native)
    assert all(len(row["description"]) > 45 for row in settings_catalog())
    assert validate_options({key: DEFAULTS[key] for key in EDITABLE})


@pytest.mark.parametrize(
    "options",
    [
        {"path": "elsewhere"},
        {"read_only": True},
        {"partitions_per_table": 1},
        {"metrics_destination": "arbitrary-file"},
        {"allow_remote_metrics": True},
        {"buffer_budget_bytes": 1},
        {"future_option": 1},
        {"max_result_rows": 0},
        {"max_result_rows": True},
        {"max_result_rows": 1.5},
        {"lease_timeout_seconds": float("nan")},
        {"lease_timeout_seconds": -1},
        {"max_transaction_bytes": 2**53},
        {"codec": "unknown"},
    ],
)
def test_invalid_or_managed_options_are_refused_before_save(options):
    with pytest.raises(ValidationError):
        RuntimeSettingsPayload(kg_grafx_options=options)
    with pytest.raises(ValidationError):
        CommunitySettings(_env_file=None, kg_grafx_options=options)


def test_env_json_and_nullable_limits_are_validated(monkeypatch):
    monkeypatch.setenv(
        "KG_GRAFX_OPTIONS", '{"max_result_rows": null, "lease_timeout_seconds": 12.5}'
    )
    configured = CommunitySettings(_env_file=None)
    assert configured.kg_grafx_options == {
        "max_result_rows": None,
        "lease_timeout_seconds": 12.5,
    }
    assert service._validate_runtime_setting_value(
        "kg_grafx_options", '{"max_result_rows": null}'
    ) == {"max_result_rows": None}


@pytest.mark.parametrize("value", [True, False, 8192.0, 8192.5, "8192"])
def test_page_geometry_requires_integer_json_without_coercion(value):
    with pytest.raises(ValidationError):
        RuntimeSettingsPayload(kg_grafx_page_size=value)
    if not isinstance(value, str):
        with pytest.raises(ValueError):
            service._validate_runtime_setting_value("kg_grafx_page_size", value)


def test_page_geometry_persisted_text_keeps_its_integer_contract():
    assert service._validate_runtime_setting_value("kg_grafx_page_size", "8192") == 8192
    assert RuntimeSettingsPayload(kg_grafx_page_size=8192).kg_grafx_page_size == 8192
    with pytest.raises(ValueError):
        service._validate_runtime_setting_value("kg_grafx_page_size", "8192.5")


@pytest.mark.asyncio
async def test_save_read_restart_and_clear_options_without_mutating_active_snapshot(
    monkeypatch, tmp_path
):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(AppSetting.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    configured = [CommunitySettings(_env_file=None, data_dir=str(tmp_path))]
    monkeypatch.setattr(service, "get_settings", lambda: configured[0])
    monkeypatch.setattr(
        service, "configure_settings", lambda value: configured.__setitem__(0, value)
    )
    monkeypatch.setattr(service, "get_session_factory", lambda: factory)
    monkeypatch.setattr(service, "_validate_runtime_settings_via_port", lambda _: None)

    async def effective():
        return {key: getattr(configured[0], key) for key in service.RUNTIME_KEYS}

    monkeypatch.setattr(service, "_read_effective_runtime_settings", effective)
    monkeypatch.setattr(service, "_boot_snapshot", {})
    desired = {"max_result_rows": 900, "lease_timeout_seconds": 12.5}
    try:
        async with factory() as db:
            response = await service.put_runtime_settings(
                db,
                {
                    "kg_grafx_buffer_pool_mb": 128,
                    "kg_grafx_options": desired,
                },
            )
            RuntimeSettingsResponse(**response)
            assert response["kg_grafx_options"] == {}
            assert response["desired_values"]["kg_grafx_options"] == desired
            assert response["restart_required"] is True
            saved = await db.get(AppSetting, "kg_grafx_options")
            assert '"max_result_rows": 900' in saved.value
        await service.apply_persisted_settings_to_core_settings()
        assert configured[0].kg_grafx_options == desired
        assert configured[0].kg_grafx_buffer_pool_mb == 128
        calls = []

        def connect(path, **options):
            calls.append(options)
            return SimpleNamespace(
                path=str(path),
                identity=SimpleNamespace(page_size=8192),
                descriptor_revalidation=options["descriptor_revalidation"],
                close=lambda: None,
            )

        for read_only in (False, True):
            pool = CommunityGrafxDatabasePool(
                tmp_path,
                connect=connect,
                read_only=read_only,
                buffer_pool_mb=configured[0].kg_grafx_buffer_pool_mb,
                constructor_options=configured[0].kg_grafx_options,
            )
            pool.get(tmp_path / "managed", page_size=8192)
            assert calls[-1]["max_result_rows"] == 900
            assert calls[-1]["lease_timeout_seconds"] == 12.5
            assert calls[-1]["buffer_budget_bytes"] == 128 * 1024**2
            pool.close_all()
        async with factory() as db:
            response = await service.get_runtime_settings(db)
            assert response["restart_required"] is False
            response = await service.put_runtime_settings(db, {"kg_grafx_options": {}})
            assert response["desired_values"]["kg_grafx_options"] == {}
            assert response["kg_grafx_options"] == desired
        await service.apply_persisted_settings_to_core_settings()
        assert configured[0].kg_grafx_options == {}
    finally:
        await engine.dispose()


def test_native_pool_receives_query_limits(tmp_path):
    from okto_grafx.errors import GrafxQueryBudgetExceeded

    pool = CommunityGrafxDatabasePool(
        tmp_path, constructor_options={"max_result_rows": 1}
    )
    try:
        db = pool.get(tmp_path / "native", page_size=8192)
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE Item(id STRING, PRIMARY KEY(id))")
            tx.execute("CREATE (:Item {id:'one'})")
            tx.execute("CREATE (:Item {id:'two'})")
        with pytest.raises(GrafxQueryBudgetExceeded):
            db.execute("MATCH (n:Item) RETURN n.id")
    finally:
        pool.close_all()


def test_all_editable_options_reach_composed_writer_and_reader_lanes(tmp_path):
    from okto_pulse.community.adapters.routed_graph_composition import (
        build_community_routed_graph_composition,
    )

    options = {key: DEFAULTS[key] for key in EDITABLE}
    settings = CommunitySettings(
        _env_file=None, data_dir=str(tmp_path), kg_grafx_options=options
    )
    calls = []

    def connect(path, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            path=str(path),
            identity=SimpleNamespace(page_size=8192),
            descriptor_revalidation=kwargs["descriptor_revalidation"],
            close=lambda: None,
        )

    bundle = build_community_routed_graph_composition(
        settings=settings, grafx_connect=connect
    )
    assert bundle.global_graph.grafx_pool is bundle.board.grafx_pool
    for pool in (bundle.board.grafx_pool, *bundle.board.grafx_read_pools):
        try:
            from pathlib import Path

            pool.get(
                Path(settings.kg_base_dir) / "constructor-inventory", page_size=8192
            )
            assert {key: calls[-1][key] for key in EDITABLE} == options
        finally:
            pool.close_all()


def test_app_recomposes_graph_after_hydrating_saved_settings_before_seed():
    import ast
    from pathlib import Path
    import okto_pulse.community.main as main

    tree = ast.parse(Path(main.__file__).read_text(encoding="utf-8"))
    lifespan = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "combined_lifespan"
    )
    calls = {
        node.func.id: node
        for node in ast.walk(lifespan)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert (
        calls["apply_persisted_settings_to_core_settings"].lineno
        < calls["configure_community_kg_registry"].lineno
    )
    assert (
        calls["configure_community_kg_registry"].lineno
        < calls["seed_community_defaults"].lineno
    )
    settings_arg = next(
        arg.value
        for arg in calls["configure_community_kg_registry"].keywords
        if arg.arg == "settings"
    )
    assert "get_settings_snapshot" in ast.unparse(settings_arg)
