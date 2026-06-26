"""R16-C — Community DataBootstrapper adapter + core port.

Covers the 7 test scenarios 1:1:

  ts_8d495739 — contract imports in isolation (subprocess: no sqlalchemy /
                infra.database / community in sys.modules).
  ts_26bd0c7a — ledger covers the 4 data-bootstrap domains in init_db order.
  ts_71673acb — idempotent replay preserves presets/flags (re-run -> skipped).
  ts_533312dd — discovery intents preserve tool_binding/params_schema/
                min_permission/is_seed on rerun.
  ts_5a7b50e2 — boundary GATE: data-bootstrap step_ids are disjoint from the
                R16-B schema-migration ledger (cross-check both ways).
  ts_c2790a33 — fail-closed (failing step / invalid plan / absent bootstrapper).
  ts_5154c83c — conformance: isinstance + canonical DTOs (exact field sets,
                no parallel DTOs).

Tests are synchronous; async bootstrap funcs are driven via ``asyncio.run`` in
a single loop per test (same approach as R16-B).
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Importing the core app registers every ORM model on Base.metadata so init_db
# builds the full schema and the bootstrap funcs find their tables. It creates
# no engine (create_database is only called inside create_app()).
import okto_pulse.core.app as _core_app  # noqa: F401
import okto_pulse.core.infra.database as _db_mod
from okto_pulse.community.adapters.data_bootstrapper import (
    CommunityDataBootstrapper,
    build_community_data_bootstrap_ledger,
    make_community_data_bootstrapper,
)
from okto_pulse.community.adapters.relational_schema_migrator import (
    build_community_migration_ledger,
)
from okto_pulse.core.ports import (
    BOOTSTRAP_DOMAINS,
    DataBootstrapError,
    DataBootstrapPlan,
    DataBootstrapResult,
    DataBootstrapStep,
    DataBootstrapStepResult,
    DataBootstrapper,
    require_bootstrapper,
)

CORE_SRC = Path(_db_mod.__file__).parents[3]  # .../okto_labs_pulse_core/src
COMMUNITY_SRC = Path(__file__).resolve().parents[1] / "src"

_DATA_BOOTSTRAP_STEP_IDS = (
    "_seed_builtin_presets",
    "_reconcile_builtin_presets",
    "_reconcile_agent_permission_flags",
    "_bootstrap_default_discovery_intents",
)


@pytest.fixture
def _isolate_engine():
    saved_engine = _db_mod._engine
    saved_factory = _db_mod._session_factory
    try:
        yield
    finally:
        _db_mod._engine = saved_engine
        _db_mod._session_factory = saved_factory


async def _snapshot(engine) -> dict:
    from sqlalchemy import text

    async with engine.connect() as conn:
        presets = (await conn.execute(text("SELECT count(*) FROM permission_presets"))).scalar()
        di = (await conn.execute(text("SELECT count(*) FROM discovery_intents"))).scalar()
        row = (
            await conn.execute(
                text(
                    "SELECT tool_binding, params_schema, min_permission, is_seed "
                    "FROM discovery_intents WHERE name = 'coverage_for_fr'"
                )
            )
        ).first()
    return {"presets": presets, "di": di, "coverage": tuple(row) if row else None}


def _det_bootstrapper(callables, steps=None) -> CommunityDataBootstrapper:
    if steps is None:
        steps = (
            DataBootstrapStep("seed_a", 1, "community", "presets", True),
            DataBootstrapStep("perm_b", 2, "community", "permissions", True),
        )
    return CommunityDataBootstrapper(steps=steps, callables=callables)


# ===========================================================================
# ts_8d495739 — contract imports in isolation.
# ===========================================================================
def test_ts_8d495739_contract_imports_in_isolation(tmp_path):
    code = (
        "import sys\n"
        "from okto_pulse.core.ports import DataBootstrapper, DataBootstrapPlan, "
        "DataBootstrapResult, require_bootstrapper\n"
        "import okto_pulse.core.ports.data_bootstrapper as m\n"
        "leaked = [\n"
        "    name for name in sys.modules\n"
        "    if name.split('.')[0] == 'sqlalchemy'\n"
        "    or name == 'okto_pulse.core.infra.database'\n"
        "    or name.startswith('okto_pulse.community')\n"
        "]\n"
        "assert not leaked, 'contract leaked heavy imports: ' + repr(leaked)\n"
        "assert DataBootstrapPlan is m.DataBootstrapPlan\n"
        "print('ISOLATION_OK')\n"
    )
    env = dict(os.environ)
    # Only the two src roots on the path; run from an empty cwd so nothing else
    # leaks in. (Not `-I`/`-E`: those would also ignore PYTHONPATH.)
    env["PYTHONPATH"] = os.pathsep.join([str(CORE_SRC), str(COMMUNITY_SRC)])
    env.pop("PYTHONSTARTUP", None)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
        timeout=90,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "ISOLATION_OK" in proc.stdout


# ===========================================================================
# ts_26bd0c7a — ledger covers the 4 domains in init_db order.
# ===========================================================================
def test_ts_26bd0c7a_ledger_four_domains_in_order():
    ledger = build_community_data_bootstrap_ledger()
    assert [s.step_id for s in ledger] == list(_DATA_BOOTSTRAP_STEP_IDS)
    assert [s.order for s in ledger] == [1, 2, 3, 4]
    assert [s.domain for s in ledger] == [
        "presets", "presets", "permissions", "discovery_intents"
    ]
    assert all(s.owner == "community" and s.idempotent for s in ledger)
    # Domains are drawn from the canonical set.
    assert {s.domain for s in ledger} == set(BOOTSTRAP_DOMAINS)


def test_ts_26bd0c7a_ledger_matches_init_db_bootstrap_order():
    """The data-bootstrap order mirrors the tail of init_db (after the schema
    region): seed -> reconcile presets -> reconcile permissions -> discovery."""
    init_src = ast.get_source_segment(
        Path(_db_mod.__file__).read_text(encoding="utf-8"),
        next(
            n for n in ast.walk(ast.parse(Path(_db_mod.__file__).read_text(encoding="utf-8")))
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "init_db"
        ),
    )
    seen: list[str] = []
    for line in init_src.splitlines():
        for sid in _DATA_BOOTSTRAP_STEP_IDS:
            if f"await {sid}(" in line:
                seen.append(sid)
    assert seen == list(_DATA_BOOTSTRAP_STEP_IDS)


# ===========================================================================
# ts_71673acb — idempotent replay preserves presets/flags.
# ===========================================================================
def test_ts_71673acb_idempotent_replay_preserves_presets_and_flags(
    tmp_path, _isolate_engine
):
    async def drive():
        _db_mod.create_database(f"sqlite+aiosqlite:///{tmp_path / 'idem.db'}")
        await _db_mod.init_db()
        before = await _snapshot(_db_mod.get_engine())

        bootstrapper = make_community_data_bootstrapper()
        plan = bootstrapper.plan(target="idem")
        r1 = await bootstrapper.aexecute(plan)
        after1 = await _snapshot(_db_mod.get_engine())
        # Same instance -> all steps skipped (no re-run, no drift).
        r2 = await bootstrapper.aexecute(plan)
        after2 = await _snapshot(_db_mod.get_engine())
        # Fresh instance -> the REAL bootstrap funcs actually re-run idempotently.
        fresh = make_community_data_bootstrapper()
        r3 = await fresh.aexecute(fresh.plan(target="idem2"))
        after3 = await _snapshot(_db_mod.get_engine())
        await _db_mod.get_engine().dispose()
        return before, after1, after2, after3, r1, r2, r3

    before, after1, after2, after3, r1, r2, r3 = asyncio.run(drive())

    assert before["presets"] == 7 and before["di"] == 14  # init_db seeded

    assert r1.is_success
    assert len(r1.applied_steps) == 4 and not r1.skipped_steps
    assert after1 == before  # presets/flags/intents preserved, no drift

    assert r2.is_success
    assert not r2.applied_steps and len(r2.skipped_steps) == 4
    assert after2 == before

    assert r3.is_success  # fresh instance re-ran the real funcs idempotently
    assert after3 == before


def test_ts_71673acb_permission_flags_merge_default_and_preserve(
    tmp_path, _isolate_engine
):
    # tr_e9908b28 / ac_1293f19a: the permissions-domain step
    # (_reconcile_agent_permission_flags) must BACKFILL registry keys missing
    # from an agent's stored flags as True, PRESERVE existing custom leaf values
    # (never overwrite), and stay idempotent on rerun.
    import copy

    from okto_pulse.core.infra.permissions import PERMISSION_REGISTRY
    from okto_pulse.core.models.db import Agent

    # Registry leaves are all True; build a partial stored tree with two edits:
    #   * an existing leaf flipped to a custom False (must be preserved);
    #   * a whole top-level subtree dropped (must be backfilled, all True).
    partial = copy.deepcopy(PERMISSION_REGISTRY)
    assert "board" in partial and "read" in partial["board"]
    assert "profile" in partial  # a small top-level subtree to drop
    partial["board"]["read"] = False  # custom value -> must be preserved
    del partial["profile"]            # missing subtree -> must be backfilled True

    async def _load_flags():
        from sqlalchemy import select

        async with _db_mod.get_session_factory()() as s:
            agent = (
                await s.execute(
                    select(Agent).where(Agent.api_key == "r16c-perm-key")
                )
            ).scalar_one()
            return copy.deepcopy(agent.permission_flags)

    async def drive():
        _db_mod.create_database(f"sqlite+aiosqlite:///{tmp_path / 'perm.db'}")
        await _db_mod.init_db()
        async with _db_mod.get_session_factory()() as session:
            session.add(
                Agent(
                    name="r16c-perm-agent",
                    api_key="r16c-perm-key",
                    api_key_hash="x",
                    created_by="r16c-test",
                    permission_flags=copy.deepcopy(partial),
                )
            )
            await session.commit()

        # Run the data bootstrapper (executes _reconcile_agent_permission_flags).
        b1 = make_community_data_bootstrapper()
        await b1.aexecute(b1.plan(target="perm"))
        after1 = await _load_flags()
        # Idempotent rerun via a fresh instance (the real func re-runs).
        b2 = make_community_data_bootstrapper()
        await b2.aexecute(b2.plan(target="perm2"))
        after2 = await _load_flags()
        await _db_mod.get_engine().dispose()
        return after1, after2

    after1, after2 = asyncio.run(drive())

    # Missing subtree backfilled (default True), present and all-True.
    assert "profile" in after1
    assert after1["profile"]["update"] is True
    # Custom False leaf preserved — NOT overwritten back to True.
    assert after1["board"]["read"] is False
    # Every registry top-level key is now present.
    for key in PERMISSION_REGISTRY:
        assert key in after1, f"registry key {key!r} not backfilled"
    # Idempotent: a second reconcile changes nothing.
    assert after2 == after1


# ===========================================================================
# ts_533312dd — discovery intents preserve their attributes on rerun.
# ===========================================================================
def test_ts_533312dd_discovery_intents_preserved_on_rerun(tmp_path, _isolate_engine):
    async def drive():
        _db_mod.create_database(f"sqlite+aiosqlite:///{tmp_path / 'di.db'}")
        await _db_mod.init_db()
        before = await _snapshot(_db_mod.get_engine())
        bootstrapper = make_community_data_bootstrapper()
        await bootstrapper.aexecute(bootstrapper.plan(target="di"))
        after = await _snapshot(_db_mod.get_engine())
        await _db_mod.get_engine().dispose()
        return before, after

    before, after = asyncio.run(drive())
    assert before["coverage"] is not None
    tool_binding, _params, min_permission, is_seed = before["coverage"]
    # The canonical seed values survive the bootstrap rerun unchanged.
    assert tool_binding == "okto_pulse_list_test_scenarios"
    assert min_permission == "kg.query.global"
    assert bool(is_seed) is True
    assert after["coverage"] == before["coverage"]  # tool_binding/params/min_perm/is_seed
    assert after["di"] == before["di"]  # no duplicate rows on rerun


# ===========================================================================
# ts_5a7b50e2 — boundary gate: data-bootstrap vs schema-migration ledgers are
# disjoint (cross-checked both ways against R16-B).
# ===========================================================================
def test_ts_5a7b50e2_data_and_schema_ledgers_are_disjoint():
    data_ids = {s.step_id for s in build_community_data_bootstrap_ledger()}
    schema_ids = {s.step_id for s in build_community_migration_ledger()}

    # No overlap in either direction.
    assert data_ids & schema_ids == set(), f"overlap: {data_ids & schema_ids}"

    # The 4 data-bootstrap funcs are NOT in the schema ledger.
    for sid in _DATA_BOOTSTRAP_STEP_IDS:
        assert sid in data_ids
        assert sid not in schema_ids

    # _migrate_agent_permissions is SCHEMA (R16-B), NOT data bootstrap — even
    # though it runs late in init_db's bootstrap region.
    assert "_migrate_agent_permissions" in schema_ids
    assert "_migrate_agent_permissions" not in data_ids


# ===========================================================================
# ts_c2790a33 — fail-closed.
# ===========================================================================
def test_ts_c2790a33_failing_step_yields_partial_never_success():
    def ok():
        return None

    def boom():
        raise RuntimeError("seed insert failed")

    bootstrapper = _det_bootstrapper({"seed_a": ok, "perm_b": boom})
    result = bootstrapper.execute(bootstrapper.plan(target="t"))

    assert not result.is_success
    assert result.status == "partial"  # seed_a applied before the failure
    assert result.failed_step is not None
    assert result.failed_step.step_id == "perm_b"
    assert result.failed_step.domain == "permissions"
    assert result.failed_step.status == "failed"
    assert "RuntimeError" in (result.failed_step.failure_reason or "")
    assert result.failed_step.remediation
    assert {s.step_id for s in result.applied_steps} == {"seed_a"}
    # Port-level fail-closed invariant.
    with pytest.raises(ValueError):
        DataBootstrapResult(status="success", failed_steps=(result.failed_step,))


def test_ts_c2790a33_first_step_failure_is_failed_not_partial():
    def boom():
        raise RuntimeError("x")

    bootstrapper = _det_bootstrapper({"seed_a": boom, "perm_b": lambda: None})
    result = bootstrapper.execute(bootstrapper.plan(target="t"))
    assert result.status == "failed"
    assert not result.applied_steps
    assert not result.is_success


def test_ts_c2790a33_missing_callable_is_fail_closed():
    bootstrapper = _det_bootstrapper({"seed_a": lambda: None})  # perm_b unbound
    result = bootstrapper.execute(bootstrapper.plan(target="t"))
    assert not result.is_success
    assert result.failed_step is not None
    assert result.failed_step.failure_reason == "no_callable_bound"


def test_ts_c2790a33_invalid_plan_raises():
    bootstrapper = make_community_data_bootstrapper()

    bad_domain = DataBootstrapPlan(
        plan_id="bad", target="t",
        steps=(DataBootstrapStep("x", 1, "community", "not_a_domain", True),),  # type: ignore[arg-type]
    )
    with pytest.raises(DataBootstrapError):
        bootstrapper.validate_plan(bad_domain)

    empty_id = DataBootstrapPlan(
        plan_id="bad", target="t",
        steps=(DataBootstrapStep("", 1, "community", "presets", True),),
    )
    with pytest.raises(DataBootstrapError):
        bootstrapper.validate_plan(empty_id)

    dup_order = DataBootstrapPlan(
        plan_id="bad", target="t",
        steps=(
            DataBootstrapStep("a", 1, "community", "presets", True),
            DataBootstrapStep("b", 1, "community", "permissions", True),
        ),
    )
    with pytest.raises(DataBootstrapError):
        bootstrapper.validate_plan(dup_order)


def test_ts_c2790a33_absent_bootstrapper_fail_closed():
    with pytest.raises(DataBootstrapError) as exc:
        require_bootstrapper(None, target="community")
    assert exc.value.failure_reason == "bootstrapper_absent"
    assert exc.value.remediation
    bootstrapper = make_community_data_bootstrapper()
    assert require_bootstrapper(bootstrapper) is bootstrapper


def test_ts_c2790a33_execute_in_running_loop_is_fail_closed():
    async def drive():
        bootstrapper = _det_bootstrapper({"seed_a": lambda: None, "perm_b": lambda: None})
        # Sync execute() inside a running loop is fail-closed (directs to aexecute).
        with pytest.raises(DataBootstrapError):
            bootstrapper.execute(bootstrapper.plan(target="t"))
        # aexecute works inside the loop.
        result = await bootstrapper.aexecute(bootstrapper.plan(target="t2"))
        return result

    result = asyncio.run(drive())
    assert result.is_success


# ===========================================================================
# ts_5154c83c — conformance: isinstance + canonical DTOs, no parallel DTOs.
# ===========================================================================
def test_ts_5154c83c_isinstance_of_port_protocol():
    bootstrapper = make_community_data_bootstrapper()
    assert isinstance(bootstrapper, DataBootstrapper)


def test_ts_5154c83c_plan_and_execute_traffic_canonical_dtos():
    bootstrapper = make_community_data_bootstrapper()
    plan = bootstrapper.plan(target="conf")
    assert type(plan) is DataBootstrapPlan
    assert all(type(s) is DataBootstrapStep for s in plan.steps)

    det = _det_bootstrapper({"seed_a": lambda: None, "perm_b": lambda: None})
    result = det.execute(det.plan(target="t"))
    assert type(result) is DataBootstrapResult
    assert result.is_success
    assert all(type(s) is DataBootstrapStepResult for s in result.applied_steps)


def test_ts_5154c83c_dto_field_sets_exact():
    assert {f.name for f in dataclasses.fields(DataBootstrapStep)} == {
        "step_id", "order", "owner", "domain", "idempotent", "metadata",
    }
    assert {f.name for f in dataclasses.fields(DataBootstrapStepResult)} == {
        "step_id", "status", "owner", "domain", "failure_reason", "remediation",
        "duration_ms", "metadata",
    }
    assert {f.name for f in dataclasses.fields(DataBootstrapPlan)} == {
        "plan_id", "target", "steps", "metadata",
    }
    assert {f.name for f in dataclasses.fields(DataBootstrapResult)} == {
        "status", "applied_steps", "skipped_steps", "failed_steps", "warnings",
        "duration_ms", "failed_step", "failure_reason", "remediation",
    }


def test_ts_5154c83c_adapter_defines_no_parallel_dtos():
    adapter_py = Path(
        __import__(
            "okto_pulse.community.adapters.data_bootstrapper",
            fromlist=["__file__"],
        ).__file__
    )
    tree = ast.parse(adapter_py.read_text(encoding="utf-8"))
    class_names = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    assert class_names == {"CommunityDataBootstrapper"}
    for forbidden in {
        "DataBootstrapStep", "DataBootstrapStepResult",
        "DataBootstrapPlan", "DataBootstrapResult",
    }:
        assert forbidden not in class_names

    # The DTOs it traffics are the canonical port classes (identity check).
    step = build_community_data_bootstrap_ledger()[0]
    assert step.__class__ is DataBootstrapStep
    assert step.__class__.__module__ == "okto_pulse.core.ports.data_bootstrapper"
