"""Health refuses incomplete observations before decoding unbounded Spec payloads."""

import json
import time

import pytest
from sqlalchemy import event

from okto_pulse.community.adapters import materialization_health as module
from okto_pulse.community.adapters.sqlalchemy_models import Board, Spec
from okto_pulse.core.kg.materialization_health import CensusStatus, HealthProbeDeadline
from test_materialization_health_adapters import _database


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["rows", "bytes", "unicode_bytes", "aggregate_bytes"])
async def test_census_refuses_volume_before_decision_classification(tmp_path, monkeypatch, kind):
    decoded = []

    def decode(value):
        decoded.append(value)
        return json.loads(value)

    engine, factory = await _database(tmp_path, "bounded-census.db", json_deserializer=decode)
    monkeypatch.setattr(module, "_CENSUS_MAX_SPEC_ROWS", 2, raising=False)
    monkeypatch.setattr(module, "_CENSUS_MAX_SPEC_BYTES", 10000 if kind == "rows" else 256, raising=False)
    try:
        async with factory() as session:
            session.add(Board(id="board", name="Board", owner_id="owner"))
            for i in range(3 if kind == "rows" else 2 if kind == "aggregate_bytes" else 1):
                title = {"rows": "small", "bytes": "x" * 512,
                         "unicode_bytes": "界" * 100, "aggregate_bytes": "x" * 100}[kind]
                session.add(Spec(id=f"spec-{i}", board_id="board", title=title,
                                 created_by="owner", decisions=[{"id": "decision", "title": "Active"}]))
            await session.commit()
        decoded.clear()
        statements = []
        event.listen(engine.sync_engine, "before_cursor_execute",
                     lambda conn, cursor, statement, params, context, many: statements.append(statement))
        classified = []
        original = module.decision_sources_from_spec
        monkeypatch.setattr(module, "decision_sources_from_spec",
                            lambda row: classified.append(row) or original(row))
        result = await module.CommunitySqlAlchemyMaterializationCensus(factory).snapshot(
            "board", generation="generation", deadline=HealthProbeDeadline(time.monotonic() + 10))
        assert result.status is CensusStatus.UNAVAILABLE
        assert result.reason_code == "board_census_volume_exceeded"
        assert all(value is None for value in result.counts.values())
        assert not classified
        assert not decoded
        assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
        # SQL gates JSON before the driver's deserializer, not after .all().
        assert any("CASE WHEN" in statement.upper() for statement in statements)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_census_exact_under_budget_ignores_foreign_board_volume(tmp_path, monkeypatch):
    engine, factory = await _database(tmp_path, "bounded-exact.db")
    monkeypatch.setattr(module, "_CENSUS_MAX_SPEC_ROWS", 2, raising=False)
    monkeypatch.setattr(module, "_CENSUS_MAX_SPEC_BYTES", 512, raising=False)
    try:
        async with factory() as session:
            session.add_all([Board(id=b, name=b, owner_id="owner") for b in ("board", "foreign", "empty")])
            session.add(Spec(id="spec", board_id="board", title="Normal", created_by="owner",
                             decisions=[{"id": "active", "title": "Active"},
                                        {"id": "old", "title": "Old", "status": "superseded"}]))
            session.add(Spec(id="spec-two", board_id="board", title="Second", created_by="owner"))
            session.add(Spec(id="foreign-spec", board_id="foreign", title="x" * 10000, created_by="owner"))
            await session.commit()
        for board, count in (("board", 3), ("empty", 0)):
            result = await module.CommunitySqlAlchemyMaterializationCensus(factory).snapshot(
                board, generation="generation", deadline=HealthProbeDeadline(time.monotonic() + 10))
            assert result.status is CensusStatus.AVAILABLE
            assert result.source_count == count
            assert result.queue_depth == 0
    finally:
        await engine.dispose()
