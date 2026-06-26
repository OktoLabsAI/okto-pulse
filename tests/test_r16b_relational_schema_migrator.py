"""R16-B — Community RelationalSchemaMigrator adapter (IMP2, card ad8fbb03).

Covers the 6 test scenarios 1:1:

  ts_7aacc71a — ledger covers ALL current _migrate_* (mechanical AST gate).
  ts_5283c465 — golden replay: adapter plan vs baseline init_db schema.
  ts_7d52dffc — idempotent replay: re-run -> skipped, no drift.
  ts_7c1fc064 — fail-closed: failing step / invalid plan / absent migrator.
  ts_35ad79e3 — layer gate: core/ports pure, core !-> community, init_db intact.
  ts_83050921 — conformance: isinstance + canonical DTOs + no parallel DTOs.

Tests are synchronous; async migrations are driven via ``asyncio.run`` inside a
single loop per test to avoid cross-loop aiosqlite engine issues.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

# Importing the core app module registers every ORM model on Base.metadata
# (the production-faithful way), so create_all builds the full schema and the
# raw-SQL _migrate_* find their columns. It does NOT create an engine
# (create_database is only called inside create_app()).
import okto_pulse.core.app as _core_app  # noqa: F401
import okto_pulse.core.infra.database as _db_mod
import okto_pulse.core.ports.relational_schema_migrator as _port_mod
from okto_pulse.community.adapters.relational_schema_migrator import (
    CREATE_ALL_BOUNDARY_STEP_ID,
    CommunityRelationalSchemaMigrator,
    build_community_migration_ledger,
    make_community_relational_schema_migrator,
)
from okto_pulse.core.ports import (
    MigrationPlan,
    MigrationResult,
    MigrationStep,
    RelationalSchemaMigrator,
    SchemaMigrationError,
    require_migrator,
)

DATABASE_PY = Path(_db_mod.__file__)
PORT_PY = Path(_port_mod.__file__)
CORE_PACKAGE_DIR = DATABASE_PY.parents[1]  # .../okto_pulse/core

_DATA_BOOTSTRAP_FUNCS = {
    "_seed_builtin_presets",
    "_reconcile_builtin_presets",
    "_reconcile_agent_permission_flags",
    "_bootstrap_default_discovery_intents",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _async_migrate_names_from_database() -> set[str]:
    """AST scan: every ``async def _migrate_*`` in database.py."""
    tree = ast.parse(DATABASE_PY.read_text(encoding="utf-8"))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name.startswith("_migrate_")
    }


def _init_db_effective_order() -> list[str]:
    """Parse init_db's body into the ordered sequence of _migrate_* calls +
    the create_all boundary marker (data bootstrap excluded — not _migrate_*)."""
    source = DATABASE_PY.read_text(encoding="utf-8")
    tree = ast.parse(source)
    init_db = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "init_db"
    )
    ordered: list[str] = []

    class _Visitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
            func = node.func
            # await _migrate_xxx()
            if isinstance(func, ast.Name) and func.id.startswith("_migrate_"):
                ordered.append(func.id)
            # await conn.run_sync(Base.metadata.create_all)
            if isinstance(func, ast.Attribute) and func.attr == "run_sync":
                for arg in node.args:
                    if (
                        isinstance(arg, ast.Attribute)
                        and arg.attr == "create_all"
                    ):
                        ordered.append(CREATE_ALL_BOUNDARY_STEP_ID)
            self.generic_visit(node)

    _Visitor().visit(init_db)
    return ordered


async def _collect_schema(engine) -> dict[str, list[str]]:
    from sqlalchemy import inspect as sa_inspect

    def _inspect(sync_conn):
        insp = sa_inspect(sync_conn)
        return {
            t: sorted(c["name"] for c in insp.get_columns(t))
            for t in sorted(insp.get_table_names())
        }

    async with engine.connect() as conn:
        return await conn.run_sync(_inspect)


@pytest.fixture
def _isolate_engine():
    """Snapshot/restore the core module-global engine + session factory so the
    DB-driving tests never leak a temp engine into sibling tests."""
    saved_engine = _db_mod._engine
    saved_factory = _db_mod._session_factory
    try:
        yield
    finally:
        _db_mod._engine = saved_engine
        _db_mod._session_factory = saved_factory


def _det_migrator(callables, steps=None):
    """Build a deterministic migrator over small synthetic steps + sync callables."""
    if steps is None:
        steps = (
            MigrationStep("pre_a", 1, "pre_create_all", "d", True, False, "community"),
            MigrationStep(CREATE_ALL_BOUNDARY_STEP_ID, 2, "create_all_boundary", "d", True, False, "community"),
            MigrationStep("post_b", 3, "post_create_all", "d", True, False, "community"),
        )
    return CommunityRelationalSchemaMigrator(steps=steps, callables=callables)


# ===========================================================================
# ts_7aacc71a — ledger gate (mechanical: count + names + order + exclusions).
# ===========================================================================
def test_ts_7aacc71a_ledger_covers_all_migrate_functions():
    migrate_names = _async_migrate_names_from_database()
    ledger = build_community_migration_ledger()
    ledger_migrate_ids = {
        s.step_id for s in ledger if s.phase != "create_all_boundary"
    }

    # 1:1 coverage — no migration without a step, no step without a migration.
    assert ledger_migrate_ids == migrate_names, (
        "ledger drift: "
        f"missing_steps={sorted(migrate_names - ledger_migrate_ids)} "
        f"orphan_steps={sorted(ledger_migrate_ids - migrate_names)}"
    )
    assert len(migrate_names) == 33, f"expected 33 _migrate_*, found {len(migrate_names)}"
    assert len(ledger_migrate_ids) == 33

    # Exactly ONE create_all_boundary step.
    boundary = [s for s in ledger if s.phase == "create_all_boundary"]
    assert len(boundary) == 1
    assert boundary[0].step_id == CREATE_ALL_BOUNDARY_STEP_ID

    # Data bootstrap is excluded — a schema plan never absorbs seeding.
    for excluded in _DATA_BOOTSTRAP_FUNCS:
        assert excluded not in ledger_migrate_ids


def test_ts_7aacc71a_ledger_order_matches_init_db_effective_order():
    ledger = build_community_migration_ledger()
    ledger_order = [s.step_id for s in sorted(ledger, key=lambda s: s.order)]
    assert _init_db_effective_order() == ledger_order


def test_ts_7aacc71a_drop_spec_skills_is_the_only_destructive():
    ledger = build_community_migration_ledger()
    destructive = {s.step_id for s in ledger if s.destructive}
    assert destructive == {"_migrate_drop_spec_skills"}
    # _migrate_agent_permissions carries the documented bootstrap-region nuance.
    perms = next(s for s in ledger if s.step_id == "_migrate_agent_permissions")
    assert perms.phase == "post_create_all"
    assert perms.metadata.get("runs_in_bootstrap_region") is True


# ===========================================================================
# ts_5283c465 — golden replay: adapter plan reproduces the init_db schema and
# does not alter an already-init_db'd baseline.
# ===========================================================================
def test_ts_5283c465_golden_replay_matches_baseline(tmp_path, _isolate_engine):
    async def drive():
        # Baseline: real init_db on DB1.
        _db_mod.create_database(f"sqlite+aiosqlite:///{tmp_path / 'baseline.db'}")
        await _db_mod.init_db()
        baseline_schema = await _collect_schema(_db_mod.get_engine())
        await _db_mod.get_engine().dispose()

        # Adapter: execute the plan (real _migrate_* + create_all) on a fresh DB2.
        _db_mod.create_database(f"sqlite+aiosqlite:///{tmp_path / 'adapter.db'}")
        migrator = make_community_relational_schema_migrator()
        plan = migrator.plan(target="golden")
        result = await migrator.aexecute(plan)
        adapter_schema = await _collect_schema(_db_mod.get_engine())
        await _db_mod.get_engine().dispose()
        return baseline_schema, adapter_schema, result

    baseline_schema, adapter_schema, result = asyncio.run(drive())
    assert result.is_success
    # Equivalent to baseline init_db (schema = tables + columns).
    assert adapter_schema == baseline_schema
    assert baseline_schema  # sanity: non-empty schema


def test_ts_5283c465_replay_over_baseline_does_not_alter_schema(
    tmp_path, _isolate_engine
):
    async def drive():
        _db_mod.create_database(f"sqlite+aiosqlite:///{tmp_path / 'base.db'}")
        await _db_mod.init_db()
        schema_before = await _collect_schema(_db_mod.get_engine())
        # Replay the adapter plan OVER the init_db'd baseline (same DB).
        migrator = make_community_relational_schema_migrator()
        result = await migrator.aexecute(migrator.plan(target="overlay"))
        schema_after = await _collect_schema(_db_mod.get_engine())
        await _db_mod.get_engine().dispose()
        return schema_before, schema_after, result

    before, after, result = asyncio.run(drive())
    assert result.is_success
    assert after == before  # adapter replay does not alter the baseline


# ===========================================================================
# ts_7d52dffc — idempotent replay: re-run -> skipped, no drift.
# ===========================================================================
def test_ts_7d52dffc_idempotent_replay_no_drift(tmp_path, _isolate_engine):
    async def drive():
        _db_mod.create_database(f"sqlite+aiosqlite:///{tmp_path / 'idem.db'}")
        migrator_a = make_community_relational_schema_migrator()
        plan = migrator_a.plan(target="idem")

        r1 = await migrator_a.aexecute(plan)
        s1 = await _collect_schema(_db_mod.get_engine())
        # Same instance -> adapter ledger reports every step skipped (no re-run).
        r2 = await migrator_a.aexecute(plan)
        s2 = await _collect_schema(_db_mod.get_engine())
        # Fresh instance -> the REAL migrations actually re-run; must not drift.
        migrator_b = make_community_relational_schema_migrator()
        r3 = await migrator_b.aexecute(migrator_b.plan(target="idem2"))
        s3 = await _collect_schema(_db_mod.get_engine())
        await _db_mod.get_engine().dispose()
        return (r1, r2, r3, s1, s2, s3)

    r1, r2, r3, s1, s2, s3 = asyncio.run(drive())
    total = len(build_community_migration_ledger())

    # First run: everything applied.
    assert r1.is_success
    assert len(r1.applied_steps) == total
    assert not r1.skipped_steps

    # Second run (same instance): everything skipped -> no drift.
    assert r2.is_success
    assert not r2.applied_steps
    assert len(r2.skipped_steps) == total
    assert s2 == s1

    # Fresh instance: real migrations re-execute idempotently -> no drift.
    assert r3.is_success
    assert s3 == s1


# ===========================================================================
# ts_7c1fc064 — fail-closed: failing step / invalid plan / absent migrator.
# ===========================================================================
def test_ts_7c1fc064_failing_step_yields_partial_never_success():
    def ok():
        return None

    def boom():
        raise RuntimeError("ALTER failed")

    migrator = _det_migrator(
        {"pre_a": ok, CREATE_ALL_BOUNDARY_STEP_ID: ok, "post_b": boom}
    )
    result = migrator.execute(migrator.plan(target="t"))

    assert not result.is_success
    assert result.status == "partial"  # earlier steps applied
    assert result.failed_step is not None
    assert result.failed_step.step_id == "post_b"
    assert result.failed_step.phase == "post_create_all"
    assert result.failed_step.status == "failed"
    assert "RuntimeError" in (result.failed_step.failure_reason or "")
    assert result.failed_step.remediation
    assert {s.step_id for s in result.applied_steps} == {"pre_a", CREATE_ALL_BOUNDARY_STEP_ID}
    # MigrationResult fail-closed invariant: success + failed step is impossible.
    with pytest.raises(ValueError):
        MigrationResult(status="success", failed_steps=(result.failed_step,))


def test_ts_7c1fc064_first_step_failure_is_failed_not_partial():
    def boom():
        raise RuntimeError("x")

    migrator = _det_migrator(
        {"pre_a": boom, CREATE_ALL_BOUNDARY_STEP_ID: lambda: None, "post_b": lambda: None}
    )
    result = migrator.execute(migrator.plan(target="t"))
    assert result.status == "failed"  # nothing applied before the failure
    assert not result.applied_steps
    assert not result.is_success


def test_ts_7c1fc064_missing_callable_is_fail_closed():
    migrator = _det_migrator({"pre_a": lambda: None})  # boundary + post unbound
    result = migrator.execute(migrator.plan(target="t"))
    assert not result.is_success
    assert result.failed_step is not None
    assert result.failed_step.failure_reason == "no_callable_bound"


def test_ts_7c1fc064_invalid_plan_raises_schema_migration_error():
    migrator = make_community_relational_schema_migrator()

    # Two create_all boundaries.
    two_boundaries = MigrationPlan(
        plan_id="bad", target="t",
        steps=(
            MigrationStep("a", 1, "create_all_boundary", "d", True, False, "c"),
            MigrationStep("b", 2, "create_all_boundary", "d", True, False, "c"),
        ),
    )
    with pytest.raises(SchemaMigrationError):
        migrator.validate_plan(two_boundaries)

    # Empty step_id.
    empty_id = MigrationPlan(
        plan_id="bad", target="t",
        steps=(MigrationStep("", 1, "pre_create_all", "d", True, False, "c"),),
    )
    with pytest.raises(SchemaMigrationError):
        migrator.validate_plan(empty_id)

    # Phase out of order (post before boundary by order).
    out_of_order = MigrationPlan(
        plan_id="bad", target="t",
        steps=(
            MigrationStep("p", 1, "post_create_all", "d", True, False, "c"),
            MigrationStep(CREATE_ALL_BOUNDARY_STEP_ID, 2, "create_all_boundary", "d", True, False, "c"),
        ),
    )
    with pytest.raises(SchemaMigrationError):
        migrator.validate_plan(out_of_order)


def test_ts_7c1fc064_absent_migrator_fail_closed():
    with pytest.raises(SchemaMigrationError) as exc:
        require_migrator(None, target="community")
    assert exc.value.failure_reason == "migrator_absent"
    assert exc.value.remediation
    migrator = make_community_relational_schema_migrator()
    assert require_migrator(migrator) is migrator  # present -> passthrough


# ===========================================================================
# ts_35ad79e3 — layer gate.
# ===========================================================================
def _imported_modules(py_path: Path) -> set[str]:
    tree = ast.parse(py_path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_ts_35ad79e3_core_ports_is_pure_no_sqlalchemy_no_community():
    imported = _imported_modules(PORT_PY)
    for mod in imported:
        low = mod.lower()
        assert "sqlalchemy" not in low, f"core/ports imports sqlalchemy: {mod!r}"
        assert "okto_pulse.community" not in low, f"core/ports imports community: {mod!r}"
        assert "infra.database" not in low, f"core/ports imports infra.database: {mod!r}"


def test_ts_35ad79e3_core_does_not_import_community():
    offenders: list[str] = []
    for py in CORE_PACKAGE_DIR.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        try:
            for mod in _imported_modules(py):
                if mod.startswith("okto_pulse.community"):
                    offenders.append(f"{py}: {mod}")
        except SyntaxError:
            continue
    assert offenders == [], f"core imports community: {offenders}"


def test_ts_35ad79e3_init_db_and_create_all_intact():
    # database.py / init_db present and not reordered (effective order == ledger).
    assert DATABASE_PY.exists()
    order = _init_db_effective_order()
    assert CREATE_ALL_BOUNDARY_STEP_ID in order, "create_all boundary missing from init_db"
    assert order[: order.index(CREATE_ALL_BOUNDARY_STEP_ID)], "no pre_create_all migrations"
    ledger_order = [
        s.step_id for s in sorted(build_community_migration_ledger(), key=lambda s: s.order)
    ]
    assert order == ledger_order  # not removed / not reordered


def test_ts_35ad79e3_adapter_module_is_layer_isolated():
    adapter_py = Path(
        __import__(
            "okto_pulse.community.adapters.relational_schema_migrator",
            fromlist=["__file__"],
        ).__file__
    )
    imported = _imported_modules(adapter_py)
    # Top-level imports: only the pure core.ports contract — no sqlalchemy,
    # no infra.database, no engine (those are lazy inside the factory).
    for mod in imported:
        low = mod.lower()
        assert "sqlalchemy" not in low, f"adapter top-level imports sqlalchemy: {mod!r}"
        assert "infra.database" not in low, f"adapter top-level imports infra.database: {mod!r}"
    assert any(m == "okto_pulse.core.ports" for m in imported)


# ===========================================================================
# ts_83050921 — conformance.
# ===========================================================================
def test_ts_83050921_isinstance_of_port_protocol():
    migrator = make_community_relational_schema_migrator()
    assert isinstance(migrator, RelationalSchemaMigrator)


def test_ts_83050921_plan_and_execute_traffic_canonical_dtos():
    migrator = make_community_relational_schema_migrator()
    plan = migrator.plan(target="conf")
    assert type(plan) is MigrationPlan
    assert all(type(s) is MigrationStep for s in plan.steps)

    # execute returns the canonical MigrationResult (deterministic small plan).
    det = _det_migrator(
        {"pre_a": lambda: None, CREATE_ALL_BOUNDARY_STEP_ID: lambda: None, "post_b": lambda: None}
    )
    result = det.execute(det.plan(target="t"))
    assert type(result) is MigrationResult
    assert result.is_success
    assert all(type(s) is MigrationStep for s in build_community_migration_ledger())


def test_ts_83050921_adapter_defines_no_parallel_dtos():
    adapter_py = Path(
        __import__(
            "okto_pulse.community.adapters.relational_schema_migrator",
            fromlist=["__file__"],
        ).__file__
    )
    tree = ast.parse(adapter_py.read_text(encoding="utf-8"))
    class_names = {
        n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)
    }
    # The adapter declares ONLY its implementation class — no parallel DTOs.
    assert class_names == {"CommunityRelationalSchemaMigrator"}
    for forbidden in {"MigrationStep", "MigrationPlan", "MigrationResult", "MigrationStepResult"}:
        assert forbidden not in class_names

    # The DTOs it traffics are the canonical port classes (identity check).
    step = build_community_migration_ledger()[0]
    assert step.__class__ is MigrationStep
    assert step.__class__.__module__ == "okto_pulse.core.ports.relational_schema_migrator"
