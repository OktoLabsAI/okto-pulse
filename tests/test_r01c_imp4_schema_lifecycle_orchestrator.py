"""R01C REPLAN-IMP4 — Community relational schema-lifecycle orchestrator.

The orchestrator (``CommunityRelationalSchemaLifecycleOrchestrator``) composes the
R16-B ``CommunityRelationalSchemaMigrator`` (schema region) and the R16-C
``CommunityDataBootstrapper`` (data-bootstrap region) and, once registered on the
core seam, MOVES the ``init_db`` lifecycle ownership to the Community edition
(FR3/FR5). Registering chooses the clean ports composition (decision IMP4-B):
schema plan FULLY, then data plan. That differs from ``init_db``'s effective
order in exactly ONE adjacent position — ``_migrate_agent_permissions`` runs
BEFORE ``_seed_builtin_presets`` instead of after.

This module is the EXECUTABLE EQUIVALENCE PROOF Codex required to accept B over
A (msg_ca3cd133). The four conditions, 1:1 to the tests:

  #1 disjoint tables / no shared observable state  -> ``test_disjoint_*`` +
     ``test_commutativity_*`` (the swapped pair COMMUTES).
  #2 _migrate_agent_permissions stays before _reconcile_agent_permission_flags
     -> ``test_migrate_agent_permissions_precedes_reconcile`` +
        ``test_composed_order_differs_from_inline_only_by_one_swap``.
  #3 empty replay inline-vs-orchestrator -> same tables/indices/columns/seeds
     -> ``test_empty_replay_orchestrator_matches_inline_init_db``.
  #4 legacy replay preserves rows/data, idempotent, no rename
     -> ``test_legacy_replay_preserves_rows_migrates_and_idempotent``.

Plus the wiring/fail-open/fail-closed contract of the seam itself.

Tests are synchronous and drive the async lifecycle via ``asyncio.run`` inside a
single loop per test (mirrors test_r16b/test_r16c) to avoid cross-loop aiosqlite
engine issues. The ``_isolate`` fixture snapshots/restores the core engine +
session-factory globals AND the schema-lifecycle seam so nothing leaks.
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

import pytest

# Import the core app module so EVERY ORM model is registered on Base.metadata
# (the production-faithful way) — create_all then builds the full schema and the
# raw-SQL _migrate_* find their columns. It does NOT create an engine.
import okto_pulse.core.app as _core_app  # noqa: F401
import okto_pulse.core.infra.database as _db_mod
import okto_pulse.core.infra.schema_lifecycle as _seam
from okto_pulse.community.adapters.relational_schema_lifecycle import (
    CommunityRelationalSchemaLifecycleOrchestrator,
    make_community_relational_schema_lifecycle_orchestrator,
    register_community_relational_schema_lifecycle,
)
from okto_pulse.community.adapters import (
    build_community_data_bootstrap_ledger,
    build_community_migration_ledger,
)
from okto_pulse.core.models.db import Agent, Board
from okto_pulse.core.ports import (
    DataBootstrapError,
    DataBootstrapPlan,
    DataBootstrapResult,
    DataBootstrapStepResult,
    MigrationPlan,
    MigrationResult,
    MigrationStepResult,
    SchemaMigrationError,
)

DATABASE_PY = Path(_db_mod.__file__)

# Composed effective order under decision B: the orchestrator runs the schema
# plan FULLY, then the data plan.
_SCHEMA_IDS = [s.step_id for s in sorted(build_community_migration_ledger(), key=lambda s: s.order)]
_DATA_IDS = [s.step_id for s in sorted(build_community_data_bootstrap_ledger(), key=lambda s: s.order)]
_COMPOSED_ORDER = _SCHEMA_IDS + _DATA_IDS

_LIFECYCLE_PREFIXES = ("_migrate_", "_seed_", "_reconcile_", "_bootstrap_")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _init_db_full_call_order() -> list[str]:
    """Parse init_db's body into the ordered sequence of lifecycle calls
    (_migrate_*/_seed_*/_reconcile_*/_bootstrap_*) + the create_all boundary
    marker. The seam-delegation branch at the top is ignored (its calls —
    ``resolve_*`` / ``initialize_schema`` — match none of the prefixes)."""
    tree = ast.parse(DATABASE_PY.read_text(encoding="utf-8"))
    init_db = next(
        n for n in ast.walk(tree)
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
    presets = [r[0] for r in await _fetch(
        engine, "SELECT name FROM permission_presets ORDER BY name"
    )]
    intents = [r[0] for r in await _fetch(
        engine, "SELECT name FROM discovery_intents ORDER BY name"
    )]
    return {"presets": presets, "intents": intents}


async def _agents_state(engine) -> dict[str, object]:
    """agent id -> permission_flags (None for legacy, else the parsed dict)."""
    out: dict[str, object] = {}
    for aid, flags in await _fetch(
        engine, "SELECT id, permission_flags FROM agents ORDER BY id"
    ):
        out[aid] = None if flags is None else json.loads(flags)
    return out


async def _presets_state(engine) -> list[str]:
    return [r[0] for r in await _fetch(
        engine, "SELECT name FROM permission_presets ORDER BY name"
    )]


async def _create_all_only() -> None:
    """Build the full schema via create_all (no migrations, no bootstrap) so a
    test can drive the two steps-under-test in a controlled order."""
    async with _db_mod.get_engine().begin() as conn:
        await conn.run_sync(_db_mod.Base.metadata.create_all)


async def _force_null_flags(agent_id: str = "a-legacy") -> None:
    """Force a TRUE SQL NULL on permission_flags.

    The ORM JSON type serializes Python ``None`` to the JSON literal ``'null'``
    (TEXT), which is NOT ``IS NULL`` — so ``_migrate_agent_permissions`` (which
    selects ``permission_flags.is_(None)``) would skip an ORM-inserted None.
    Production legacy rows predate the column (added later via
    ``_migrate_add_permission_columns`` with no default) and are real SQL NULLs.
    This reproduces that faithfully so the migration actually fires."""
    from sqlalchemy import text

    async with _db_mod.get_engine().begin() as conn:
        await conn.execute(
            text("UPDATE agents SET permission_flags = NULL WHERE id = :id"),
            {"id": agent_id},
        )


async def _insert_legacy_agent(api_key: str = "legacy-key") -> None:
    """Insert ONE agent with a real SQL-NULL permission_flags (legacy flat
    permissions) — gives _migrate_agent_permissions real work."""
    async with _db_mod.get_session_factory()() as s:
        s.add(Agent(
            id="a-legacy", name="Legacy Agent", api_key=api_key,
            api_key_hash="legacy-hash", created_by="owner-1",
            permissions=["read", "write"], permission_flags=None,
        ))
        await s.commit()
    await _force_null_flags("a-legacy")


@pytest.fixture
def _isolate():
    """Snapshot/restore the core engine + session-factory globals AND the
    schema-lifecycle seam, starting each test from a fail-open (None) seam."""
    saved_engine = _db_mod._engine
    saved_factory = _db_mod._session_factory
    saved_orch = _seam.resolve_relational_schema_lifecycle_orchestrator()
    _seam.reset_relational_schema_lifecycle_orchestrator()
    try:
        yield
    finally:
        _db_mod._engine = saved_engine
        _db_mod._session_factory = saved_factory
        if saved_orch is not None:
            _seam.register_relational_schema_lifecycle_orchestrator(saved_orch)
        else:
            _seam.reset_relational_schema_lifecycle_orchestrator()


# ===========================================================================
# Wiring: seam registration + init_db delegation (FR3) + fail-open default.
# ===========================================================================
def test_register_helper_sets_the_core_seam(_isolate):
    orch = register_community_relational_schema_lifecycle()
    assert isinstance(orch, CommunityRelationalSchemaLifecycleOrchestrator)
    assert _seam.resolve_relational_schema_lifecycle_orchestrator() is orch


def test_init_db_delegates_to_registered_orchestrator(tmp_path, _isolate):
    """init_db delegates the WHOLE lifecycle to a registered orchestrator and
    does NOT run its inline body (spied via _migrate_card_statuses)."""
    calls = {"orchestrator": 0, "inline_migrate": 0}

    async def drive():
        _db_mod.create_database(f"sqlite+aiosqlite:///{tmp_path / 'deleg.db'}")

        class _Spy:
            async def initialize_schema(self) -> None:
                calls["orchestrator"] += 1

        _seam.register_relational_schema_lifecycle_orchestrator(_Spy())
        orig = _db_mod._migrate_card_statuses

        async def _tracked():
            calls["inline_migrate"] += 1
            return await orig()

        _db_mod._migrate_card_statuses = _tracked
        try:
            await _db_mod.init_db()
        finally:
            _db_mod._migrate_card_statuses = orig
            await _db_mod.get_engine().dispose()

    asyncio.run(drive())
    assert calls["orchestrator"] == 1   # delegated
    assert calls["inline_migrate"] == 0  # inline body bypassed


def test_init_db_runs_inline_when_no_orchestrator_registered(tmp_path, _isolate):
    """Fail-open: with nothing registered, init_db runs its unchanged inline
    body (register-before-remove — the core fallback is intact)."""
    calls = {"inline_migrate": 0}

    async def drive():
        _seam.reset_relational_schema_lifecycle_orchestrator()
        _db_mod.create_database(f"sqlite+aiosqlite:///{tmp_path / 'inline.db'}")
        orig = _db_mod._migrate_card_statuses

        async def _tracked():
            calls["inline_migrate"] += 1
            return await orig()

        _db_mod._migrate_card_statuses = _tracked
        try:
            await _db_mod.init_db()
        finally:
            _db_mod._migrate_card_statuses = orig
            await _db_mod.get_engine().dispose()

    asyncio.run(drive())
    assert calls["inline_migrate"] == 1


# ===========================================================================
# Condition #3 — empty replay: inline init_db vs orchestrator produce the SAME
# tables, indices, columns and observable seeds.
# ===========================================================================
def test_empty_replay_orchestrator_matches_inline_init_db(tmp_path, _isolate):
    async def drive():
        # Baseline: real inline init_db (seam stays None) on DB-A.
        _seam.reset_relational_schema_lifecycle_orchestrator()
        _db_mod.create_database(f"sqlite+aiosqlite:///{tmp_path / 'inline.db'}")
        await _db_mod.init_db()
        inline_schema = await _collect_schema(_db_mod.get_engine())
        inline_seeds = await _seed_names(_db_mod.get_engine())
        await _db_mod.get_engine().dispose()

        # Orchestrator: register + init_db delegates (exercises the wired path)
        # on a fresh DB-B.
        _db_mod.create_database(f"sqlite+aiosqlite:///{tmp_path / 'orch.db'}")
        register_community_relational_schema_lifecycle()
        await _db_mod.init_db()
        orch_schema = await _collect_schema(_db_mod.get_engine())
        orch_seeds = await _seed_names(_db_mod.get_engine())
        await _db_mod.get_engine().dispose()
        return inline_schema, inline_seeds, orch_schema, orch_seeds

    inline_schema, inline_seeds, orch_schema, orch_seeds = asyncio.run(drive())
    assert inline_schema  # sanity: non-empty
    assert orch_schema == inline_schema      # same tables + columns + indices
    assert orch_seeds == inline_seeds        # same presets + discovery intents
    assert inline_seeds["presets"]           # sanity: seeds non-empty
    assert inline_seeds["intents"]


# ===========================================================================
# Condition #1 — disjoint tables / no shared observable state.
# ===========================================================================
def test_disjoint_seed_touches_only_presets_migrate_only_agents(tmp_path, _isolate):
    async def drive():
        # seed-only run -> agents untouched, presets change.
        _db_mod.create_database(f"sqlite+aiosqlite:///{tmp_path / 'seed_only.db'}")
        await _create_all_only()
        await _insert_legacy_agent()
        a_before = await _agents_state(_db_mod.get_engine())
        p_before = await _presets_state(_db_mod.get_engine())
        await _db_mod._seed_builtin_presets()
        a_after = await _agents_state(_db_mod.get_engine())
        p_after = await _presets_state(_db_mod.get_engine())
        await _db_mod.get_engine().dispose()

        # migrate-only run -> presets untouched, agent flags change.
        _db_mod.create_database(f"sqlite+aiosqlite:///{tmp_path / 'mig_only.db'}")
        await _create_all_only()
        await _insert_legacy_agent()
        a2_before = await _agents_state(_db_mod.get_engine())
        p2_before = await _presets_state(_db_mod.get_engine())
        await _db_mod._migrate_agent_permissions()
        a2_after = await _agents_state(_db_mod.get_engine())
        p2_after = await _presets_state(_db_mod.get_engine())
        await _db_mod.get_engine().dispose()
        return (a_before, a_after, p_before, p_after,
                a2_before, a2_after, p2_before, p2_after)

    (a_before, a_after, p_before, p_after,
     a2_before, a2_after, p2_before, p2_after) = asyncio.run(drive())

    # _seed_builtin_presets writes ONLY permission_presets.
    assert a_after == a_before            # agents untouched by seed
    assert p_after != p_before and p_after  # presets seeded
    # _migrate_agent_permissions writes ONLY agents.
    assert p2_after == p2_before          # presets untouched by migrate
    assert a2_after != a2_before          # agent flags migrated
    assert a2_before["a-legacy"] is None and a2_after["a-legacy"] is not None


def test_commutativity_migrate_and_seed_yield_identical_state(tmp_path, _isolate):
    """The swapped pair COMMUTES: [migrate, seed] and [seed, migrate] from an
    identical start produce identical final agents AND identical final presets."""
    async def run_order(db_name: str, order: list[str]):
        _db_mod.create_database(f"sqlite+aiosqlite:///{tmp_path / db_name}")
        await _create_all_only()
        await _insert_legacy_agent()
        for fn_name in order:
            await getattr(_db_mod, fn_name)()
        agents = await _agents_state(_db_mod.get_engine())
        presets = await _presets_state(_db_mod.get_engine())
        await _db_mod.get_engine().dispose()
        return agents, presets

    async def drive():
        mig_first = await run_order(
            "mig_first.db", ["_migrate_agent_permissions", "_seed_builtin_presets"]
        )
        seed_first = await run_order(
            "seed_first.db", ["_seed_builtin_presets", "_migrate_agent_permissions"]
        )
        return mig_first, seed_first

    (mig_agents, mig_presets), (seed_agents, seed_presets) = asyncio.run(drive())
    assert mig_agents == seed_agents      # agents identical regardless of order
    assert mig_presets == seed_presets    # presets identical regardless of order
    # And both actually did the work (not a vacuous pass).
    assert mig_agents["a-legacy"] is not None
    assert mig_presets


# ===========================================================================
# Condition #2 — _migrate_agent_permissions stays BEFORE
# _reconcile_agent_permission_flags, and the composed order differs from inline
# init_db by exactly ONE adjacent swap (the proven-commuting pair).
# ===========================================================================
def test_migrate_agent_permissions_precedes_reconcile():
    assert (
        _COMPOSED_ORDER.index("_migrate_agent_permissions")
        < _COMPOSED_ORDER.index("_reconcile_agent_permission_flags")
    )
    # It is the LAST schema-region step (tail of the migrator plan).
    assert _SCHEMA_IDS[-1] == "_migrate_agent_permissions"
    # The same invariant holds in init_db's inline order.
    inline = _init_db_full_call_order()
    assert (
        inline.index("_migrate_agent_permissions")
        < inline.index("_reconcile_agent_permission_flags")
    )


def test_composed_order_differs_from_inline_only_by_one_swap():
    inline = _init_db_full_call_order()
    # Same steps, no add/drop: the orchestrator neither absorbs nor sheds a step.
    assert sorted(_COMPOSED_ORDER) == sorted(inline)
    diff_positions = [
        i for i, (c, n) in enumerate(zip(_COMPOSED_ORDER, inline)) if c != n
    ]
    # Exactly two positions differ, and they are an adjacent transposition of
    # {_migrate_agent_permissions, _seed_builtin_presets}.
    assert len(diff_positions) == 2
    i, j = diff_positions
    assert j == i + 1                                   # adjacent
    assert {_COMPOSED_ORDER[i], _COMPOSED_ORDER[j]} == {
        "_migrate_agent_permissions", "_seed_builtin_presets"
    }
    assert _COMPOSED_ORDER[i] == inline[j] and _COMPOSED_ORDER[j] == inline[i]


# ===========================================================================
# Condition #4 — legacy replay: rows/data preserved, migrated, idempotent, no
# table/column rename or loss.
# ===========================================================================
def test_legacy_replay_preserves_rows_migrates_and_idempotent(tmp_path, _isolate):
    async def _rows(engine):
        boards = sorted(r[0] for r in await _fetch(engine, "SELECT id FROM boards"))
        agents = await _agents_state(engine)
        seeds = await _seed_names(engine)
        return {
            "board_ids": boards,
            "agent_ids": sorted(agents),
            "agent_flags": agents,
            "preset_count": len(seeds["presets"]),
            "intent_count": len(seeds["intents"]),
        }

    async def drive():
        _db_mod.create_database(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
        # 1) First full lifecycle -> full schema + seeds.
        await make_community_relational_schema_lifecycle_orchestrator().initialize_schema()
        schema0 = await _collect_schema(_db_mod.get_engine())
        # 2) Seed LEGACY user data: a board + an agent with NULL permission_flags
        #    (legacy flat permissions) that predates the granular-flags migration.
        async with _db_mod.get_session_factory()() as s:
            s.add(Board(id="b-legacy", name="Legacy Board", owner_id="owner-1"))
            s.add(Agent(
                id="a-legacy", name="Legacy Agent", api_key="legacy-key",
                api_key_hash="legacy-hash", created_by="owner-1",
                permissions=["read", "write"], permission_flags=None,
            ))
            await s.commit()
        await _force_null_flags("a-legacy")  # real SQL NULL: legacy pre-column row
        rows0 = await _rows(_db_mod.get_engine())
        # 3) Replay the lifecycle (fresh orchestrator) over the legacy DB.
        await make_community_relational_schema_lifecycle_orchestrator().initialize_schema()
        schema1 = await _collect_schema(_db_mod.get_engine())
        rows1 = await _rows(_db_mod.get_engine())
        # 4) Replay AGAIN -> idempotent.
        await make_community_relational_schema_lifecycle_orchestrator().initialize_schema()
        schema2 = await _collect_schema(_db_mod.get_engine())
        rows2 = await _rows(_db_mod.get_engine())
        await _db_mod.get_engine().dispose()
        return schema0, schema1, schema2, rows0, rows1, rows2

    schema0, schema1, schema2, rows0, rows1, rows2 = asyncio.run(drive())

    # No table/column rename or loss across replays (schema stable).
    assert schema1 == schema0
    assert schema2 == schema0
    # Rows preserved by id (no loss, no rename of the row's identity).
    assert rows0["board_ids"] == ["b-legacy"]
    assert rows1["board_ids"] == ["b-legacy"]
    assert rows1["agent_ids"] == rows0["agent_ids"] == ["a-legacy"]
    # The legacy agent was MIGRATED in place: NULL flags -> populated.
    assert rows0["agent_flags"]["a-legacy"] is None
    assert rows1["agent_flags"]["a-legacy"] is not None
    # Idempotent: a further replay changes nothing (rows + flags + seed counts).
    assert rows2 == rows1
    assert rows2["preset_count"] == rows1["preset_count"] == rows0["preset_count"]
    assert rows2["intent_count"] == rows1["intent_count"] == rows0["intent_count"]


# ===========================================================================
# Fail-closed: a failed/partial migration or bootstrap result is re-raised as
# the port's structured error — the lifecycle never silently half-applies.
# ===========================================================================
def _ok_schema_result() -> MigrationResult:
    return MigrationResult(status="success")


class _OkMigrator:
    def plan(self, *, target):
        return MigrationPlan(plan_id="ok", target=target)

    def validate_plan(self, plan):  # pragma: no cover - trivial
        return None

    async def aexecute(self, plan):
        return _ok_schema_result()


class _BoomMigrator:
    async def aexecute(self, plan):
        return MigrationResult.failed_result(
            MigrationStepResult(
                step_id="post_boom", status="failed", phase="post_create_all",
                failure_reason="schema_boom", remediation="repair the migration",
            )
        )

    def plan(self, *, target):
        return MigrationPlan(plan_id="boom", target=target)


class _BoomBootstrapper:
    called = False

    async def aexecute(self, plan):
        type(self).called = True
        return DataBootstrapResult.failed_result(
            DataBootstrapStepResult(
                step_id="seed_boom", status="failed", owner="community",
                domain="presets", failure_reason="bootstrap_boom",
                remediation="repair the seed",
            )
        )

    def plan(self, *, target):
        return DataBootstrapPlan(plan_id="boom", target=target)


def test_orchestrator_fail_closed_on_schema_failure():
    boot = _BoomBootstrapper()
    boot.__class__.called = False
    orch = CommunityRelationalSchemaLifecycleOrchestrator(
        migrator=_BoomMigrator(), bootstrapper=boot,
    )
    with pytest.raises(SchemaMigrationError) as ei:
        asyncio.run(orch.initialize_schema())
    assert ei.value.failure_reason == "schema_boom"
    assert ei.value.step_id == "post_boom"
    # Data region must NOT run when the schema region failed.
    assert _BoomBootstrapper.called is False


def test_orchestrator_fail_closed_on_bootstrap_failure():
    orch = CommunityRelationalSchemaLifecycleOrchestrator(
        migrator=_OkMigrator(), bootstrapper=_BoomBootstrapper(),
    )
    with pytest.raises(DataBootstrapError) as ei:
        asyncio.run(orch.initialize_schema())
    assert ei.value.failure_reason == "bootstrap_boom"
    assert ei.value.step_id == "seed_boom"
    assert ei.value.domain == "presets"
