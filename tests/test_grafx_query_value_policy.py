"""Community query-value ceiling; native Grafx remains independently configurable."""

import json

import pytest
from okto_grafx.runtime.config import DatabaseConfig
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.community.adapters import sqlalchemy_runtime_settings_service as service
from okto_pulse.community.adapters.grafx_database_pool import CommunityGrafxDatabasePool
from okto_pulse.community.adapters.grafx_settings_catalog import settings_catalog, validate_options
from okto_pulse.community.adapters.sqlalchemy_models import AppSetting
from okto_pulse.community.config import CommunitySettings


KEY = "max_query_value_characters"


def test_default_and_catalog_bounds_match_effective_native_default():
    row = next(row for row in settings_catalog() if row["name"] == KEY)
    assert row["default"] == DatabaseConfig(path=":memory:").max_query_value_characters == 65536
    assert (row["minimum"], row["maximum"]) == (1, 65536)
    assert row["editable"] and not row["nullable"]
    assert validate_options({}) == {}
    # This is a Pulse product policy, not a restriction on other Grafx consumers.
    assert DatabaseConfig(path=":memory:", max_query_value_characters=1048576).max_query_value_characters == 1048576


@pytest.mark.parametrize("limit", [1, 32768, 65536])
def test_valid_limits_across_admission_paths(limit, monkeypatch, tmp_path):
    options = {KEY: limit}
    assert validate_options(options) == options
    assert CommunitySettings(_env_file=None, kg_grafx_options=options).kg_grafx_options == options
    assert service._validate_runtime_setting_value("kg_grafx_options", json.dumps(options)) == options
    monkeypatch.setenv("KG_GRAFX_OPTIONS", json.dumps(options))
    assert CommunitySettings(_env_file=None).kg_grafx_options == options
    pool = CommunityGrafxDatabasePool(tmp_path, constructor_options=options)
    assert pool.constructor_options == options


@pytest.mark.parametrize("limit", [65537, 1048576, 0, -1, True, 65536.0, "65536", None])
def test_invalid_limits_cannot_bypass_deployment_persistence_or_pool(limit, monkeypatch, tmp_path):
    options = {KEY: limit}
    for admit in (
        validate_options,
        lambda value: CommunitySettings(_env_file=None, kg_grafx_options=value),
        lambda value: service._validate_runtime_setting_value("kg_grafx_options", json.dumps(value)),
        lambda value: CommunityGrafxDatabasePool(tmp_path, constructor_options=value),
    ):
        with pytest.raises(ValueError, match="between 1 and 65536 in Pulse"):
            admit(options)
    monkeypatch.setenv("KG_GRAFX_OPTIONS", json.dumps(options))
    with pytest.raises(ValueError, match="between 1 and 65536 in Pulse"):
        CommunitySettings(_env_file=None)


@pytest.mark.asyncio
async def test_legacy_persisted_oversized_options_are_not_applied_or_silently_rewritten(caplog):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(AppSetting.__table__.create)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            raw = json.dumps({KEY: 65537, "max_result_rows": 100})
            db.add(AppSetting(key="kg_grafx_options", value=raw))
            await db.commit()
            assert "kg_grafx_options" not in await service._load_persisted_rows(db)
            assert (await db.get(AppSetting, "kg_grafx_options")).value == raw
            assert "settings.invalid_persisted_value" in caplog.text
    finally:
        await engine.dispose()


@pytest.mark.parametrize("options", [{}, {KEY: 65536}, {KEY: 1024}])
def test_native_writes_reads_and_rollback_respect_configured_character_boundary(tmp_path, options):
    from okto_grafx.errors import GrafxConfigurationError

    limit = options.get(KEY, 65536)
    # Unicode proves the bound counts characters, not UTF-8 bytes.
    payload = "é" * limit
    path = tmp_path / "boundary"
    writer_pool = CommunityGrafxDatabasePool(tmp_path, constructor_options=options)
    reader_pool = CommunityGrafxDatabasePool(tmp_path, read_only=True, constructor_options=options)
    try:
        writer = writer_pool.get(path, page_size=8192)
        with writer.begin("write") as tx:
            tx.execute("CREATE NODE TABLE Note(id STRING, body STRING, PRIMARY KEY(id))")
            tx.execute("CREATE (:Note {id: 'one', body: $body})", {"body": payload})
        with pytest.raises(GrafxConfigurationError):
            with writer.begin("write") as tx:
                tx.execute("MATCH (n:Note) SET n.body = 'rolled back'")
                tx.execute("MATCH (n:Note) SET n.body = $body", {"body": payload + "é"})
        assert writer.execute("MATCH (n:Note) RETURN n.body").rows == ((payload,),)
        writer.checkpoint()
        reader = reader_pool.get(path, page_size=8192)
        assert reader.execute("MATCH (n:Note) RETURN n.body").rows == ((payload,),)
    finally:
        reader_pool.close_all()
        writer_pool.close_all()
    reopened = CommunityGrafxDatabasePool(tmp_path, read_only=True, constructor_options=options)
    try:
        assert reopened.get(path, page_size=8192).execute("MATCH (n:Note) RETURN n.body").rows == ((payload,),)
    finally:
        reopened.close_all()
