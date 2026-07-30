"""R16-C — Community DataBootstrapper adapter + core port.

Covers the 7 test scenarios 1:1:

  ts_8d495739 — contract imports in isolation (subprocess: no sqlalchemy /
                infra.database / community in sys.modules).
  ts_26bd0c7a — ledger covers the data-bootstrap domains in init_db order.
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
import okto_pulse.community.app as _core_app  # noqa: F401
import okto_pulse.core.infra.database as _db_mod
import okto_pulse.community.adapters.data_bootstrap_steps as _bootstrap_steps
from okto_pulse.community.adapters.data_bootstrapper import (
    CommunityDataBootstrapper,
    build_community_data_bootstrap_ledger,
    make_community_data_bootstrapper,
)
from okto_pulse.community.adapters.relational_schema_migrator import (
    build_community_migration_ledger,
)
from okto_pulse.community.adapters.relational_schema_lifecycle import (
    register_community_relational_schema_lifecycle,
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

CORE_SRC = Path(_db_mod.__file__).parents[3]  # selected Core checkout /src
COMMUNITY_SRC = Path(__file__).resolve().parents[1] / "src"

_DATA_BOOTSTRAP_STEP_IDS = (
    "_seed_builtin_presets",
    "_reconcile_builtin_presets",
    "_reconcile_agent_permission_flags",
    "_bootstrap_default_discovery_intents",
    "_backfill_knowledge_propagation_v2",
    "_bootstrap_quality_assessment_legacy_import_v1",
)


@pytest.fixture
def _isolate_engine():
    yield


async def _snapshot(engine) -> dict:
    from sqlalchemy import text

    async with engine.connect() as conn:
        presets = (
            await conn.execute(text("SELECT count(*) FROM permission_presets"))
        ).scalar()
        di = (
            await conn.execute(text("SELECT count(*) FROM discovery_intents"))
        ).scalar()
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
# ts_26bd0c7a — ledger covers the canonical domains in init_db order.
# ===========================================================================
def test_ts_26bd0c7a_ledger_domains_in_order():
    ledger = build_community_data_bootstrap_ledger()
    assert [s.step_id for s in ledger] == list(_DATA_BOOTSTRAP_STEP_IDS)
    assert [s.order for s in ledger] == [1, 2, 3, 4, 5, 6]
    assert [s.domain for s in ledger] == [
        "presets",
        "presets",
        "permissions",
        "discovery_intents",
        "knowledge_propagation",
        "quality_assessment",
    ]
    assert all(s.owner == "community" and s.idempotent for s in ledger)
    # Domains are drawn from the canonical set.
    assert {s.domain for s in ledger} == set(BOOTSTRAP_DOMAINS)


def test_ts_26bd0c7a_ledger_matches_community_bootstrap_registry():
    """The data-bootstrap ledger mirrors the concrete Community registry."""
    assert list(_bootstrap_steps.DATA_BOOTSTRAP_STEP_CALLABLES) == list(
        _DATA_BOOTSTRAP_STEP_IDS
    )


# ===========================================================================
# ts_71673acb — idempotent replay preserves presets/flags.
# ===========================================================================
def test_ts_71673acb_idempotent_replay_preserves_presets_and_flags(
    tmp_path, _isolate_engine
):
    async def drive():
        _db_mod.create_database(f"sqlite+aiosqlite:///{tmp_path / 'idem.db'}")
        register_community_relational_schema_lifecycle()
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
    assert len(r1.applied_steps) == 4
    assert {step.step_id for step in r1.skipped_steps} == {
        "_backfill_knowledge_propagation_v2",
        "_bootstrap_quality_assessment_legacy_import_v1",
    }
    assert after1 == before  # presets/flags/intents preserved, no drift

    assert r2.is_success
    assert not r2.applied_steps and len(r2.skipped_steps) == 6
    assert after2 == before

    assert r3.is_success  # fresh instance re-ran the real funcs idempotently
    assert after3 == before


def test_c7_legacy_quality_bootstrap_imports_and_replays_without_drift(
    tmp_path, _isolate_engine
):
    async def quality_snapshot():
        from sqlalchemy import func, select

        from okto_pulse.community.adapters.sqlalchemy_models import (
            DomainEventHandlerExecution,
            DomainEventRow,
            QualityAssessmentHeadRow,
            QualityAssessmentLegacyImportCandidateRow,
            QualityAssessmentLegacyImportCheckpointRow,
            QualityAssessmentLegacyImportCompletionRow,
            QualityAssessmentLegacyImportResolutionRow,
            QualityAssessmentLegacyImportRunRow,
            QualityAssessmentReceiptRow,
        )

        models = (
            QualityAssessmentLegacyImportRunRow,
            QualityAssessmentLegacyImportCandidateRow,
            QualityAssessmentLegacyImportCheckpointRow,
            QualityAssessmentLegacyImportResolutionRow,
            QualityAssessmentLegacyImportCompletionRow,
            QualityAssessmentReceiptRow,
            QualityAssessmentHeadRow,
        )
        async with _db_mod.get_session_factory()() as session:
            counts = []
            for model in models:
                counts.append(
                    int(
                        await session.scalar(
                            select(func.count())
                            .select_from(model)
                            .where(model.board_id == "board-c7-bootstrap")
                        )
                        or 0
                    )
                )
            counts.append(
                int(
                    await session.scalar(
                        select(func.count())
                        .select_from(DomainEventHandlerExecution)
                        .join(
                            DomainEventRow,
                            DomainEventRow.id == DomainEventHandlerExecution.event_id,
                        )
                        .where(
                            DomainEventRow.board_id == "board-c7-bootstrap",
                            DomainEventHandlerExecution.handler_name
                            == "ConsolidationEnqueuer",
                        )
                    )
                    or 0
                )
            )
            return tuple(counts)

    async def drive():
        from okto_pulse.community.adapters.sqlalchemy_models import (
            Board,
            Ideation,
        )
        from okto_pulse.core.domain.enums import IdeationStatus

        _db_mod.create_database(
            f"sqlite+aiosqlite:///{tmp_path / 'quality-bootstrap.db'}"
        )
        register_community_relational_schema_lifecycle()
        await _db_mod.init_db()
        async with _db_mod.get_session_factory()() as session:
            session.add(
                Board(
                    id="board-c7-bootstrap",
                    name="C7 bootstrap",
                    owner_id="owner-c7-bootstrap",
                    realm_id="local",
                    settings={
                        "require_ideation_ambiguity_gate": True,
                        "max_ideation_ambiguity": 3,
                    },
                )
            )
            session.add(
                Ideation(
                    id="ideation-c7-bootstrap",
                    board_id="board-c7-bootstrap",
                    title="Legacy ambiguity",
                    status=IdeationStatus.DONE,
                    version=1,
                    scope_assessment={
                        "ambiguity": 2,
                        "ambiguity_justification": "Legacy assessment",
                    },
                    created_by="owner-c7-bootstrap",
                )
            )
            await session.commit()

        first = make_community_data_bootstrapper()
        first_result = await first.aexecute(first.plan(target="quality-1"))
        first_snapshot = await quality_snapshot()
        replay = make_community_data_bootstrapper()
        replay_result = await replay.aexecute(replay.plan(target="quality-2"))
        replay_snapshot = await quality_snapshot()
        await _db_mod.get_engine().dispose()
        return (
            first_result,
            first_snapshot,
            replay_result,
            replay_snapshot,
        )

    first_result, first_snapshot, replay_result, replay_snapshot = asyncio.run(drive())
    assert first_result.is_success, first_result.failure_reason
    assert replay_result.is_success, replay_result.failure_reason
    assert first_snapshot == (1, 1, 1, 1, 1, 1, 1, 1)
    assert replay_snapshot == first_snapshot


def test_c7_legacy_quality_bootstrap_replays_after_subject_purge(
    tmp_path, _isolate_engine
):
    async def durable_and_physical_snapshot():
        from sqlalchemy import func, select

        from okto_pulse.community.adapters.sqlalchemy_models import (
            ActivityLog,
            DomainEventRow,
            QualityAssessmentHeadRow,
            QualityAssessmentLegacyImportCompletionRow,
            QualityAssessmentOutboxRow,
            QualityAssessmentReceiptRow,
        )

        models = (
            QualityAssessmentLegacyImportCompletionRow,
            QualityAssessmentReceiptRow,
            QualityAssessmentHeadRow,
            DomainEventRow,
            QualityAssessmentOutboxRow,
        )
        async with _db_mod.get_session_factory()() as session:
            counts = [
                int(
                    await session.scalar(
                        select(func.count())
                        .select_from(model)
                        .where(model.board_id == "board-c7-purge-replay")
                    )
                    or 0
                )
                for model in models
            ]
            counts.append(
                int(
                    await session.scalar(
                        select(func.count())
                        .select_from(ActivityLog)
                        .where(
                            ActivityLog.board_id == "board-c7-purge-replay",
                            ActivityLog.action == "quality_assessment_legacy_imported",
                        )
                    )
                    or 0
                )
            )
            return tuple(counts)

    async def drive():
        from okto_pulse.community.adapters.sqlalchemy_models import (
            Board,
            Ideation,
        )
        from okto_pulse.community.adapters.sqlalchemy_quality_assessment_lifecycle import (
            CommunitySqlAlchemyQualityAssessmentLifecycle,
        )
        from okto_pulse.core.domain.enums import IdeationStatus
        from okto_pulse.core.domain.quality_assessment import (
            AssessmentSubjectType,
        )
        from okto_pulse.core.services.quality_assessment_lifecycle import (
            QualityAssessmentLifecycleService,
        )

        _db_mod.create_database(
            f"sqlite+aiosqlite:///{tmp_path / 'quality-purge-replay.db'}"
        )
        register_community_relational_schema_lifecycle()
        await _db_mod.init_db()
        async with _db_mod.get_session_factory()() as session:
            session.add(
                Board(
                    id="board-c7-purge-replay",
                    name="C7 purge replay",
                    owner_id="owner-c7-purge-replay",
                    realm_id="local",
                    settings={
                        "require_ideation_ambiguity_gate": True,
                        "max_ideation_ambiguity": 3,
                    },
                )
            )
            session.add(
                Ideation(
                    id="ideation-c7-purge-replay",
                    board_id="board-c7-purge-replay",
                    title="Legacy ambiguity",
                    status=IdeationStatus.DONE,
                    version=1,
                    scope_assessment={
                        "ambiguity": 2,
                        "ambiguity_justification": "Legacy assessment",
                    },
                    created_by="owner-c7-purge-replay",
                )
            )
            await session.commit()

        first = make_community_data_bootstrapper()
        first_result = await first.aexecute(first.plan(target="quality-first"))

        lifecycle = QualityAssessmentLifecycleService()
        purge_plan = lifecycle.prepare_subject_purge(
            board_id="board-c7-purge-replay",
            subject_type=AssessmentSubjectType.IDEATION,
            subject_id="ideation-c7-purge-replay",
        )
        async with _db_mod.get_session_factory()() as session:
            persistence = CommunitySqlAlchemyQualityAssessmentLifecycle(session)
            purge_postcondition = await persistence.apply_purge_plan(purge_plan)
            lifecycle.validate_purge_postcondition(
                plan=purge_plan,
                postcondition=purge_postcondition,
            )
            await session.commit()

        after_purge = await durable_and_physical_snapshot()
        replay = make_community_data_bootstrapper()
        replay_result = await replay.aexecute(
            replay.plan(target="quality-after-subject-purge")
        )
        after_replay = await durable_and_physical_snapshot()
        await _db_mod.get_engine().dispose()
        return first_result, after_purge, replay_result, after_replay

    first_result, after_purge, replay_result, after_replay = asyncio.run(drive())
    assert first_result.is_success, first_result.failure_reason
    # The one-shot completion ledger survives while its subject-scoped
    # receipt/event/outbox/history bundle is legitimately gone.
    assert after_purge == (1, 0, 0, 0, 0, 0)
    assert replay_result.is_success, replay_result.failure_reason
    # A startup/bootstrap replay trusts the validated durable completion and
    # neither bricks nor recreates the already-purged operational evidence.
    assert after_replay == after_purge


def test_ts_71673acb_permission_flags_stay_sparse_and_preserve_overrides(
    tmp_path, _isolate_engine
):
    # The permission-domain step must not materialize missing registry leaves.
    # Sparse explicit values/extensions are preserved and replay is idempotent.
    import copy

    from okto_pulse.core.ports.permission_policy import registered_permission_flags
    from okto_pulse.community.adapters.sqlalchemy_models import Agent

    # Build a partial pre-SK-A stored tree with:
    #   * an existing leaf flipped to a custom False (must be preserved);
    #   * a whole top-level subtree absent (must remain absent);
    #   * all SK-A/v1 leaves absent (must remain absent).
    partial = registered_permission_flags()
    assert "board" in partial and "read" in partial["board"]
    assert "profile" in partial  # a small top-level subtree to drop
    partial["board"]["read"] = False  # custom value -> must be preserved
    extension = {"mode": ["custom", False]}
    partial["vendor_extension"] = copy.deepcopy(extension)
    partial["board"]["vendor_extension"] = copy.deepcopy(extension)
    del partial["profile"]  # missing subtree -> must be backfilled True
    del partial["ideation"]["quality"]
    del partial["refinement"]["quality"]
    del partial["refinement"]["research_decisions"]
    del partial["spec"]["quality"]
    del partial["spec"]["checklist"]

    async def _load_flags():
        from sqlalchemy import select

        async with _db_mod.get_session_factory()() as s:
            agent = (
                await s.execute(select(Agent).where(Agent.api_key == "r16c-perm-key"))
            ).scalar_one()
            return copy.deepcopy(agent.permission_flags)

    async def drive():
        _db_mod.create_database(f"sqlite+aiosqlite:///{tmp_path / 'perm.db'}")
        register_community_relational_schema_lifecycle()
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

    # Missing leaves remain absent so future manifest/preset grants propagate.
    assert "profile" not in after1
    # Custom False leaf preserved — NOT overwritten back to True.
    assert after1["board"]["read"] is False
    assert "quality" not in after1["ideation"]
    assert "quality" not in after1["refinement"]
    assert "research_decisions" not in after1["refinement"]
    assert "quality" not in after1["spec"]
    assert "checklist" not in after1["spec"]
    # Non-canonical extension keys and their opaque shapes are preserved.
    assert after1["vendor_extension"] == extension
    assert after1["board"]["vendor_extension"] == extension
    # Idempotent: a second reconcile changes nothing.
    assert after2 == after1


def test_permission_upgrade_normalizes_full_control_and_preset_snapshots(
    tmp_path,
    _isolate_engine,
):
    import copy

    from sqlalchemy import select

    from okto_pulse.core.ports.permission_policy import (
        get_permission_flag,
        permission_introduction_manifests,
        registered_permission_flags,
        set_permission_flag,
        ska_permission_introduction_v1,
    )
    from okto_pulse.community.adapters.relational_application import (
        CommunityPermissionPresetGateway,
    )
    from okto_pulse.community.adapters.sqlalchemy_models import (
        Agent,
        PermissionIntroductionAudit,
        PermissionPreset,
    )

    def without_ska_branches(flags):
        result = copy.deepcopy(flags)
        del result["ideation"]["quality"]
        del result["refinement"]["quality"]
        del result["refinement"]["research_decisions"]
        del result["spec"]["quality"]
        del result["spec"]["checklist"]
        return result

    async def load_layers():
        async with _db_mod.get_session_factory()() as session:
            rows = (
                await session.execute(
                    select(Agent).where(
                        Agent.id.in_(
                            (
                                "upgrade-full",
                                "upgrade-preset",
                                "upgrade-faulty",
                                "upgrade-explicit",
                                "upgrade-extension",
                            )
                        )
                    )
                )
            ).scalars()
            return {row.id: copy.deepcopy(row.permission_flags) for row in rows}

    async def drive():
        _db_mod.create_database(f"sqlite+aiosqlite:///{tmp_path / 'upgrade.db'}")
        register_community_relational_schema_lifecycle()
        await _db_mod.init_db()
        try:
            async with _db_mod.get_session_factory()() as session:
                spec = (
                    await session.execute(
                        select(PermissionPreset).where(
                            PermissionPreset.name == "Spec",
                            PermissionPreset.is_builtin.is_(True),
                        )
                    )
                ).scalar_one()
                historical_full = without_ska_branches(registered_permission_flags())
                historical_full_with_extension = copy.deepcopy(historical_full)
                historical_full_with_extension["vendor_extension"] = {
                    "grant": False,
                    "audit": True,
                }
                historical_spec = without_ska_branches(spec.flags)
                faulty_spec = copy.deepcopy(spec.flags)
                for leaf in ska_permission_introduction_v1().leaves:
                    set_permission_flag(faulty_spec, leaf, False)
                session.add_all(
                    [
                        Agent(
                            id="upgrade-full",
                            name="Historical Full Control",
                            api_key="upgrade-full-key",
                            api_key_hash="upgrade-full-hash",
                            created_by="upgrade-full-owner",
                            permission_flags=historical_full,
                        ),
                        Agent(
                            id="upgrade-preset",
                            name="Historical preset snapshot",
                            api_key="upgrade-preset-key",
                            api_key_hash="upgrade-preset-hash",
                            created_by="upgrade-preset-owner",
                            preset_id=spec.id,
                            permission_flags=historical_spec,
                        ),
                        Agent(
                            id="upgrade-faulty",
                            name="Faulty prior introduction backfill",
                            api_key="upgrade-faulty-key",
                            api_key_hash="upgrade-faulty-hash",
                            created_by="upgrade-faulty-owner",
                            preset_id=spec.id,
                            permission_flags=faulty_spec,
                        ),
                        Agent(
                            id="upgrade-explicit",
                            name="Sparse explicit override",
                            api_key="upgrade-explicit-key",
                            api_key_hash="upgrade-explicit-hash",
                            created_by="upgrade-explicit-owner",
                            preset_id=spec.id,
                            permission_flags={"ideation": {"quality": {"read": False}}},
                        ),
                        Agent(
                            id="upgrade-extension",
                            name="Historical Full Control with extension",
                            api_key="upgrade-extension-key",
                            api_key_hash="upgrade-extension-hash",
                            created_by="upgrade-extension-owner",
                            permission_flags=historical_full_with_extension,
                        ),
                    ]
                )
                await session.commit()

            await _bootstrap_steps._reconcile_agent_permission_flags()
            after_first = await load_layers()
            await _bootstrap_steps._reconcile_agent_permission_flags()
            after_second = await load_layers()

            async with _db_mod.get_session_factory()() as session:
                gateway = CommunityPermissionPresetGateway(session)
                effective = {
                    owner: await gateway.get_effective_permissions(
                        user_id=owner,
                        board_id="no-ceiling",
                    )
                    for owner in (
                        "upgrade-full-owner",
                        "upgrade-preset-owner",
                        "upgrade-faulty-owner",
                        "upgrade-explicit-owner",
                        "upgrade-extension-owner",
                    )
                }
                audits = list(
                    (
                        await session.execute(
                            select(PermissionIntroductionAudit).order_by(
                                PermissionIntroductionAudit.created_at,
                                PermissionIntroductionAudit.id,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            return after_first, after_second, effective, audits
        finally:
            await _db_mod.get_engine().dispose()

    after_first, after_second, effective, audits = asyncio.run(drive())
    assert after_first == {
        "upgrade-explicit": {"ideation": {"quality": {"read": False}}},
        "upgrade-faulty": {},
        "upgrade-full": None,
        "upgrade-preset": {},
        "upgrade-extension": {"vendor_extension": {"grant": False, "audit": True}},
    }
    assert after_second == after_first

    manifests = permission_introduction_manifests()
    manifest_by_version = {manifest.version: manifest for manifest in manifests}
    ska_manifest = ska_permission_introduction_v1()
    assert all(
        get_permission_flag(
            effective["upgrade-full-owner"].flags,
            leaf,
        )
        is True
        for leaf in ska_manifest.leaves
    )
    extension_effective = effective["upgrade-extension-owner"]
    assert extension_effective.owner_review_required is True
    assert extension_effective.review_reason == "unrecognized_direct_permissions"
    assert extension_effective.flags["vendor_extension"] == {
        "grant": False,
        "audit": False,
    }
    spec_grants = set(ska_manifest.grants_for("Spec"))
    for owner in ("upgrade-preset-owner", "upgrade-faulty-owner"):
        assert {
            leaf
            for leaf in ska_manifest.leaves
            if get_permission_flag(effective[owner].flags, leaf) is True
        } == spec_grants
    assert (
        get_permission_flag(
            effective["upgrade-explicit-owner"].flags,
            "ideation.quality.read",
        )
        is False
    )
    assert (
        get_permission_flag(
            effective["upgrade-explicit-owner"].flags,
            "ideation.quality.assess",
        )
        is True
    )
    agent_audits = [row for row in audits if row.phase == "agent_reconciliation"]
    assert agent_audits
    assert {row.manifest_version for row in agent_audits} == set(manifest_by_version)
    assert all(
        len(row.before_digest) == 64 and len(row.after_digest) == 64
        for row in agent_audits
    )
    assert all(
        row.details["manifest_order"]
        == next(
            index
            for index, manifest in enumerate(manifests)
            if manifest.version == row.manifest_version
        )
        for row in agent_audits
    )
    extension_rows = [
        row for row in agent_audits if row.subject_id == "upgrade-extension"
    ]
    assert len(extension_rows) == 2 * len(manifests)
    assert all(
        row.classification == "direct_unrecognized"
        and row.owner_review_required is True
        and row.introduced_true_count == 0
        and row.introduced_false_count
        == len(manifest_by_version[row.manifest_version].leaves)
        for row in extension_rows
    )
    summaries = [row for row in agent_audits if row.classification == "run_summary"]
    assert len(summaries) >= 2 * len(manifests)
    for manifest in manifests:
        version_summaries = [
            row for row in summaries if row.manifest_version == manifest.version
        ]
        assert len(version_summaries) >= 2
        assert any(row.mutation_count == 0 for row in version_summaries)


@pytest.mark.parametrize(
    ("stored_value", "expected_message"),
    [
        ("{not-json", "not valid JSON"),
        ("[]", "must be a JSON object"),
        ("42", "must be a JSON object"),
        ('{"board":[]}', "canonical branch 'board'"),
        ('{"board":false}', "canonical branch 'board'"),
        ('{"board":{"read":"false"}}', "canonical leaf 'board.read'"),
    ],
    ids=(
        "malformed-json",
        "array",
        "scalar",
        "canonical-branch-array",
        "canonical-branch-scalar",
        "canonical-leaf-non-bool",
    ),
)
def test_permission_flag_reconcile_rejects_invalid_documents_and_rolls_back(
    monkeypatch, stored_value, expected_message
):
    class _Rows:
        def mappings(self):
            return self

        def all(self):
            return [{"id": "invalid-agent", "permission_flags": stored_value}]

    class _Session:
        def __init__(self):
            self.execute_count = 0
            self.commit_count = 0
            self.rollback_count = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, *args, **kwargs):
            self.execute_count += 1
            return _Rows()

        async def commit(self):
            self.commit_count += 1

        async def rollback(self):
            self.rollback_count += 1

    session = _Session()
    monkeypatch.setattr(
        _bootstrap_steps,
        "get_session_factory",
        lambda: lambda: session,
    )

    with pytest.raises(ValueError, match=expected_message):
        asyncio.run(_bootstrap_steps._reconcile_agent_permission_flags())

    assert session.execute_count == 1
    assert session.commit_count == 0
    assert session.rollback_count == 1


def test_permission_flag_reconcile_propagates_database_failure_after_rollback(
    monkeypatch,
):
    class _Session:
        def __init__(self):
            self.rollback_count = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, *args, **kwargs):
            raise RuntimeError("permission query failed")

        async def rollback(self):
            self.rollback_count += 1

    session = _Session()
    monkeypatch.setattr(
        _bootstrap_steps,
        "get_session_factory",
        lambda: lambda: session,
    )

    with pytest.raises(RuntimeError, match="permission query failed"):
        asyncio.run(_bootstrap_steps._reconcile_agent_permission_flags())

    assert session.rollback_count == 1


@pytest.mark.parametrize(
    ("invalid_flags", "expected_message"),
    [
        ({"board": []}, "canonical branch 'board'"),
        ({"board": False}, "canonical branch 'board'"),
        ({"board": {"read": "false"}}, "canonical leaf 'board.read'"),
    ],
    ids=("branch-array", "branch-bool", "leaf-string"),
)
def test_permission_flag_reconcile_rejects_invalid_canonical_shape_in_database(
    tmp_path,
    _isolate_engine,
    invalid_flags,
    expected_message,
):
    import copy

    from sqlalchemy import select

    from okto_pulse.community.adapters.sqlalchemy_models import Agent

    async def drive():
        _db_mod.create_database(f"sqlite+aiosqlite:///{tmp_path / 'shape.db'}")
        register_community_relational_schema_lifecycle()
        await _db_mod.init_db()
        try:
            async with _db_mod.get_session_factory()() as session:
                session.add(
                    Agent(
                        id="invalid-shape-agent",
                        name="Invalid Shape Agent",
                        api_key="invalid-shape-key",
                        api_key_hash="invalid-shape-hash",
                        created_by="r16c-test",
                        permission_flags=copy.deepcopy(invalid_flags),
                    )
                )
                await session.commit()

            with pytest.raises(ValueError, match=expected_message):
                await _bootstrap_steps._reconcile_agent_permission_flags()

            async with _db_mod.get_session_factory()() as session:
                stored = (
                    await session.execute(
                        select(Agent.permission_flags).where(
                            Agent.id == "invalid-shape-agent"
                        )
                    )
                ).scalar_one()
                return copy.deepcopy(stored)
        finally:
            await _db_mod.get_engine().dispose()

    assert asyncio.run(drive()) == invalid_flags


def test_permission_flag_null_storage_is_backward_compatible_on_replay(
    tmp_path,
    _isolate_engine,
):
    from sqlalchemy import text

    from okto_pulse.community.adapters.relational_application import (
        CommunityPermissionPresetGateway,
    )
    from okto_pulse.community.adapters.relational_schema_lifecycle import (
        make_community_relational_schema_lifecycle_orchestrator,
    )
    from okto_pulse.community.adapters.sqlalchemy_models import Agent

    async def raw_flags():
        async with _db_mod.get_engine().connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT id, permission_flags FROM agents "
                        "WHERE id = 'json-null-agent' "
                        "ORDER BY id"
                    )
                )
            ).all()
            return {agent_id: flags for agent_id, flags in rows}

    async def drive():
        _db_mod.create_database(f"sqlite+aiosqlite:///{tmp_path / 'null.db'}")
        register_community_relational_schema_lifecycle()
        await _db_mod.init_db()
        try:
            async with _db_mod.get_session_factory()() as session:
                preset_id = (
                    await session.execute(
                        text(
                            "SELECT id FROM permission_presets "
                            "WHERE is_builtin = 1 AND name = 'Reporter'"
                        )
                    )
                ).scalar_one()
                session.add(
                    Agent(
                        id="json-null-agent",
                        name="JSON Null Agent",
                        api_key="json-null-key",
                        api_key_hash="json-null-hash",
                        created_by="json-null-owner",
                        preset_id=preset_id,
                        permission_flags=None,
                    )
                )
                await session.commit()

            before = await raw_flags()
            # Exercise the actual startup composition (migration followed by
            # data reconciliation), not only the helper in isolation.
            orchestrator = make_community_relational_schema_lifecycle_orchestrator()
            await orchestrator.initialize_schema()
            after = await raw_flags()
            async with _db_mod.get_session_factory()() as session:
                effective = await CommunityPermissionPresetGateway(
                    session
                ).get_effective_permissions(
                    user_id="json-null-owner",
                    board_id="any-board",
                )
            return before, after, effective
        finally:
            await _db_mod.get_engine().dispose()

    before, after, effective = asyncio.run(drive())

    # The current SQLAlchemy JSON binding persists the supported Python None
    # value as a TEXT JSON literal, which the replay must treat as absence.
    assert before == {"json-null-agent": "null"}
    assert after == before
    # No full-control backfill: the restrictive preset remains authoritative.
    assert effective.preset_name == "Reporter"
    assert effective.flags["kg"]["session"]["commit"] is False


# ===========================================================================
# ts_533312dd — discovery intents preserve their attributes on rerun.
# ===========================================================================
def test_ts_533312dd_discovery_intents_preserved_on_rerun(tmp_path, _isolate_engine):
    async def drive():
        _db_mod.create_database(f"sqlite+aiosqlite:///{tmp_path / 'di.db'}")
        register_community_relational_schema_lifecycle()
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
    assert (
        after["coverage"] == before["coverage"]
    )  # tool_binding/params/min_perm/is_seed
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

    # The data-bootstrap funcs are NOT in the schema ledger.
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
        plan_id="bad",
        target="t",
        steps=(DataBootstrapStep("x", 1, "community", "not_a_domain", True),),  # type: ignore[arg-type]
    )
    with pytest.raises(DataBootstrapError):
        bootstrapper.validate_plan(bad_domain)

    empty_id = DataBootstrapPlan(
        plan_id="bad",
        target="t",
        steps=(DataBootstrapStep("", 1, "community", "presets", True),),
    )
    with pytest.raises(DataBootstrapError):
        bootstrapper.validate_plan(empty_id)

    dup_order = DataBootstrapPlan(
        plan_id="bad",
        target="t",
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
        bootstrapper = _det_bootstrapper(
            {"seed_a": lambda: None, "perm_b": lambda: None}
        )
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
        "step_id",
        "order",
        "owner",
        "domain",
        "idempotent",
        "metadata",
    }
    assert {f.name for f in dataclasses.fields(DataBootstrapStepResult)} == {
        "step_id",
        "status",
        "owner",
        "domain",
        "failure_reason",
        "remediation",
        "duration_ms",
        "metadata",
    }
    assert {f.name for f in dataclasses.fields(DataBootstrapPlan)} == {
        "plan_id",
        "target",
        "steps",
        "metadata",
    }
    assert {f.name for f in dataclasses.fields(DataBootstrapResult)} == {
        "status",
        "applied_steps",
        "skipped_steps",
        "failed_steps",
        "warnings",
        "duration_ms",
        "failed_step",
        "failure_reason",
        "remediation",
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
        "DataBootstrapStep",
        "DataBootstrapStepResult",
        "DataBootstrapPlan",
        "DataBootstrapResult",
    }:
        assert forbidden not in class_names

    # The DTOs it traffics are the canonical port classes (identity check).
    step = build_community_data_bootstrap_ledger()[0]
    assert step.__class__ is DataBootstrapStep
    assert step.__class__.__module__ == "okto_pulse.core.ports.data_bootstrapper"
