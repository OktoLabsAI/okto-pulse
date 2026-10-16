"""Current schema lifecycle: edition ownership, delegation and failure boundaries."""
import ast
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import okto_pulse.core.infra.database as _db_mod
import okto_pulse.core.ports.relational_runtime as _relational_runtime_port
import okto_pulse.core.ports.schema_lifecycle as _seam
import okto_pulse.community.adapters.relational_schema_lifecycle as lifecycle
from okto_pulse.community.adapters.relational_schema_lifecycle import (
    CommunityRelationalSchemaLifecycleOrchestrator,
    make_community_relational_schema_lifecycle_orchestrator,
    register_community_relational_schema_lifecycle,
)
from okto_pulse.community.adapters.sqlalchemy_database import configure_community_database

DATABASE_PY = Path(_relational_runtime_port.__file__)
_LIFECYCLE_PREFIXES = ("_migrate_", "_seed_", "_reconcile_", "_bootstrap_")


def _init_db_full_call_order() -> list[str]:
    """Parse init_db's body into the ordered sequence of lifecycle calls
    (_migrate_*/_seed_*/_reconcile_*/_bootstrap_*) + the create_all boundary
    marker. The seam-delegation branch at the top is ignored (its calls —
    ``resolve_*`` / ``initialize_schema`` — match none of the prefixes)."""
    tree = ast.parse(DATABASE_PY.read_text(encoding="utf-8"))
    init_db = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "init_db"
    )
    ordered: list[str] = []

    class _Visitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
            func = node.func
            if isinstance(func, ast.Name) and func.id.startswith(_LIFECYCLE_PREFIXES):
                ordered.append(func.id)
            if isinstance(func, ast.Attribute) and func.attr == "run_sync":
                for arg in node.args:
                    if isinstance(arg, ast.Attribute) and arg.attr == "create_all":
                        ordered.append("create_all_boundary")
            self.generic_visit(node)

    _Visitor().visit(init_db)
    return ordered


async def _collect_schema(engine) -> dict[str, dict[str, list]]:
    """Tables -> {columns, indexes}. Columns are sorted names; indexes are
    sorted (name, columns) pairs. Equality across two engines == identical
    physical schema (tables + columns + indices)."""
    from sqlalchemy import inspect as sa_inspect

    def _inspect(sync_conn):
        insp = sa_inspect(sync_conn)
        out: dict[str, dict[str, list]] = {}
        for t in sorted(insp.get_table_names()):
            out[t] = {
                "columns": sorted(c["name"] for c in insp.get_columns(t)),
                "indexes": sorted(
                    (ix["name"], tuple(ix.get("column_names") or ()))
                    for ix in insp.get_indexes(t)
                ),
            }
        return out

    async with engine.connect() as conn:
        return await conn.run_sync(_inspect)


async def _fetch(engine, sql: str) -> list[tuple]:
    from sqlalchemy import text

    async with engine.connect() as conn:
        res = await conn.execute(text(sql))
        return list(res.fetchall())


async def _seed_names(engine) -> dict[str, list[str]]:
    """Observable seeds: built-in preset names + discovery-intent names."""
    presets = [
        r[0]
        for r in await _fetch(
            engine, "SELECT name FROM permission_presets ORDER BY name"
        )
    ]
    intents = [
        r[0]
        for r in await _fetch(
            engine, "SELECT name FROM discovery_intents ORDER BY name"
        )
    ]
    return {"presets": presets, "intents": intents}


@pytest.fixture
def _isolate():
    """Start each test from an unregistered schema-lifecycle seam."""
    _seam.reset_relational_schema_lifecycle_orchestrator()
    try:
        yield
    finally:
        _seam.reset_relational_schema_lifecycle_orchestrator()


def test_register_helper_sets_the_core_seam(_isolate):
    orch = register_community_relational_schema_lifecycle()
    assert isinstance(orch, CommunityRelationalSchemaLifecycleOrchestrator)
    assert _seam.resolve_relational_schema_lifecycle_orchestrator() is orch


def test_init_db_delegates_to_registered_orchestrator(tmp_path, _isolate):
    """init_db delegates the WHOLE lifecycle to a registered orchestrator and
    returns without touching lifecycle SQL itself."""
    calls = {"orchestrator": 0}

    async def drive():
        _db_mod.create_database(f"sqlite+aiosqlite:///{tmp_path / 'deleg.db'}")

        class _Spy:
            async def initialize_schema(self) -> None:
                calls["orchestrator"] += 1

        _seam.register_relational_schema_lifecycle_orchestrator(_Spy())
        try:
            await _db_mod.init_db()
        finally:
            await _db_mod.get_engine().dispose()

    asyncio.run(drive())
    assert calls["orchestrator"] == 1  # delegated


def test_init_db_fails_closed_when_no_orchestrator_registered(tmp_path, _isolate):
    async def drive():
        _seam.reset_relational_schema_lifecycle_orchestrator()
        _db_mod.create_database(f"sqlite+aiosqlite:///{tmp_path / 'inline.db'}")
        try:
            with pytest.raises(RuntimeError, match="orchestrator not registered"):
                await _db_mod.init_db()
        finally:
            await _db_mod.get_engine().dispose()

    asyncio.run(drive())


def test_empty_replay_registered_init_db_matches_direct_orchestrator(
    tmp_path, _isolate
):
    async def drive():
        # Baseline: direct Community orchestrator on DB-A.
        _db_mod.create_database(f"sqlite+aiosqlite:///{tmp_path / 'direct.db'}")
        await make_community_relational_schema_lifecycle_orchestrator().initialize_schema()
        direct_schema = await _collect_schema(_db_mod.get_engine())
        direct_seeds = await _seed_names(_db_mod.get_engine())
        await _db_mod.get_engine().dispose()

        # Registered seam: init_db delegates to the same Community lifecycle.
        _db_mod.create_database(f"sqlite+aiosqlite:///{tmp_path / 'orch.db'}")
        register_community_relational_schema_lifecycle()
        await _db_mod.init_db()
        orch_schema = await _collect_schema(_db_mod.get_engine())
        orch_seeds = await _seed_names(_db_mod.get_engine())
        await _db_mod.get_engine().dispose()
        return direct_schema, direct_seeds, orch_schema, orch_seeds

    direct_schema, direct_seeds, orch_schema, orch_seeds = asyncio.run(drive())
    assert direct_schema  # sanity: non-empty
    assert orch_schema == direct_schema  # same tables + columns + indices
    assert orch_seeds == direct_seeds  # same presets + discovery intents
    assert direct_seeds["presets"]  # sanity: seeds non-empty
    assert direct_seeds["intents"]



def test_core_init_db_has_no_inline_lifecycle_order():
    assert _init_db_full_call_order() == []

@pytest.mark.parametrize("failure", ["schema", "seed"])
def test_orchestrator_propagates_failure_without_running_later_steps(tmp_path, monkeypatch, failure):
    async def drive():
        runtime = configure_community_database(f"sqlite+aiosqlite:///{tmp_path / 'failure.db'}")
        schema = AsyncMock(side_effect=RuntimeError("schema_failed") if failure == "schema" else None)
        seed = AsyncMock(side_effect=RuntimeError("seed_failed") if failure == "seed" else None)
        monkeypatch.setattr(lifecycle, "initialize_current_schema", schema)
        orchestrator = CommunityRelationalSchemaLifecycleOrchestrator(runtime_provider=lambda: runtime, seed=seed)
        try:
            with pytest.raises(RuntimeError, match=f"{failure}_failed"):
                await orchestrator.initialize_schema()
            schema.assert_awaited_once()
            if failure == "schema":
                seed.assert_not_awaited()
            else:
                seed.assert_awaited_once()
        finally:
            await runtime.engine.dispose()
    asyncio.run(drive())
