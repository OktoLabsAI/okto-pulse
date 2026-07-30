from __future__ import annotations

import sys
from pathlib import Path

import pytest
import pytest_asyncio

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = REPO_ROOT.parent
LOCAL_IMPORT_PATHS = (
    REPO_ROOT / "src",
    WORKSPACE_ROOT / "okto-pulse-core" / "src",
)

for path in reversed(LOCAL_IMPORT_PATHS):
    value = str(path)
    while value in sys.path:
        sys.path.remove(value)
    sys.path.insert(0, value)

from okto_pulse.core.application.boundary.repository_checkout import (  # noqa: E402
    activate_repository_checkout_paths,
)

# Purge stale legacy checkout roots before collection. The normalized sys.path
# and PYTHONPATH are inherited by both multiprocessing spawn and subprocess
# workers used by the storage and CLI suites.
_REPOSITORY_PATHS = activate_repository_checkout_paths(
    anchor_repo=REPO_ROOT,
    required=False,
)


@pytest.fixture(autouse=True)
def _reset_relational_schema_lifecycle_seam():
    """Isolate the process-global relational schema-lifecycle seam per test.

    R01C REPLAN-IMP4 activated the seam: the Community composition root
    (``create_community_app`` / the CLI boots) registers a
    ``RelationalSchemaLifecycleOrchestrator`` so ``init_db`` delegates the
    lifecycle to the edition. That registration is a process-global. Without
    isolation, ANY test that constructs the app leaks the registration into
    later tests. Reset to the unregistered default (None) before AND after every
    test so each test is hermetic; a test
    that needs the orchestrator registers it explicitly within its own body."""
    from okto_pulse.core import runtime_registry as _runtime_registry
    from okto_pulse.core.runtime_context import (
        capture_runtime_values_for_tests,
        reset_runtime_values,
        restore_runtime_values_for_tests,
    )
    from okto_pulse.community.adapters.sqlalchemy_database import (
        configure_community_database,
    )
    from okto_pulse.community.config import CommunitySettings
    from okto_pulse.core.infra.config import configure_settings

    saved_runtime = capture_runtime_values_for_tests()
    reset_runtime_values()
    _runtime_registry.register_relational_runtime_factory(
        lambda url, echo=False: configure_community_database(url, echo=echo)
    )
    from okto_pulse.core.ports.requirement_lint import (
        RequirementLintWriteResult,
        register_requirement_lint_writer_hook,
    )

    class _ContractTestRequirementLintHook:
        async def stage_requirement_lint(self, context, command):  # noqa: ANN001
            del context
            return RequirementLintWriteResult(
                receipt_id=(
                    f"qar_test_{command.spec_id}_{command.spec_version}_"
                    f"{command.writer.value}"
                ),
                head_revision=command.spec_version,
                evaluated_rule_count=1,
                finding_count=0,
            )

    register_requirement_lint_writer_hook(_ContractTestRequirementLintHook())
    configure_settings(CommunitySettings())
    try:
        yield
    finally:
        restore_runtime_values_for_tests(saved_runtime)


@pytest_asyncio.fixture(autouse=True, loop_scope="function")
async def _close_test_community_database_runtime(
    _reset_relational_schema_lifecycle_seam,
):
    """Dispose a test-created Community runtime before its loop is closed.

    The registry-isolation fixture above restores the caller's complete
    runtime snapshot after this dependent fixture tears down.  Consequently,
    only a runtime created inside the current test is visible here: a saved
    external runtime is neither resolved nor closed.
    """

    yield

    from okto_pulse.community.adapters.sqlalchemy_database import (
        CommunityDatabaseRuntime,
    )
    from okto_pulse.core.ports.relational_runtime import (
        is_database_runtime_configured,
        resolve_database_runtime,
    )

    if not is_database_runtime_configured():
        return
    runtime = resolve_database_runtime()
    if isinstance(runtime, CommunityDatabaseRuntime):
        await runtime.close()


def require_active_runtime_registry():
    """Test-only helper: consume the already-active runtime-value registry and
    NEVER create one (no ``create=True``). If no registry is active it fails
    typed (T-A1-4)."""
    from okto_pulse.core.runtime_context import current_runtime_values

    registry = current_runtime_values()
    if registry is None:
        raise RuntimeError("active runtime-value registry unavailable")
    return registry


@pytest.fixture(name="require_active_runtime_registry")
def _require_active_runtime_registry_fixture():
    """Passive fixture handing tests the module-level helper above, so test
    files can inject it instead of flat-importing ``conftest`` (which breaks
    under broad collection when another rootless ``conftest`` module wins the
    ``sys.modules`` slot)."""
    return require_active_runtime_registry


@pytest.fixture
def active_runtime_registry():
    """Return the ambient runtime-value registry established by the autouse
    seam fixture so tests can inject it explicitly into the Community
    cold-start transaction."""
    return require_active_runtime_registry()


@pytest.fixture
def recovery_store_factory():
    """Create lifecycle-initialized synchronous stores for adapter tests only."""

    from sqlalchemy import create_engine

    from okto_pulse.community.adapters.global_discovery_recovery_worker import (
        SQLAlchemyRecoveryRunStore,
    )
    from okto_pulse.community.adapters.sqlalchemy_models import (
        Base,
        Board,
        GlobalDiscoveryRecoveryAttempt,
        GlobalDiscoveryRecoveryDispatch,
        GlobalDiscoveryRecoverySlot,
        GlobalDiscoveryRecoveryTransition,
    )

    class _PreparedRevoker:
        def revoke_prepared(self, **_kwargs) -> None:
            return None

        def is_prepared_revoked(self, **_kwargs) -> bool:
            return False

    engines = []

    def build(database_url: str) -> SQLAlchemyRecoveryRunStore:
        engine = create_engine(
            database_url,
            future=True,
            connect_args={"check_same_thread": False, "timeout": 5.0},
        )
        Base.metadata.create_all(
            engine,
            tables=[
                Board.__table__,
                GlobalDiscoveryRecoveryAttempt.__table__,
                GlobalDiscoveryRecoverySlot.__table__,
                GlobalDiscoveryRecoveryDispatch.__table__,
                GlobalDiscoveryRecoveryTransition.__table__,
            ],
        )
        engines.append(engine)
        return SQLAlchemyRecoveryRunStore(
            engine=engine,
            prepared_revoker=_PreparedRevoker(),
        )

    yield build

    for engine in engines:
        engine.dispose()


@pytest.fixture
def prepared_recovery_admitter():
    """Stage the prepared half of a legacy worker test without rescanning."""

    from datetime import timedelta

    from okto_pulse.community.adapters.global_discovery_recovery_worker import (
        RecoveryDispatchStage,
    )
    from okto_pulse.core.kg.global_discovery_recovery_control import (
        RecoveryPreparationCommand,
        RecoveryPreparedResult,
        RecoveryRunBinding,
        RecoveryStartCommand,
    )

    def admit(store, command: RecoveryStartCommand):
        if not isinstance(command, RecoveryStartCommand):
            raise TypeError("command must be RecoveryStartCommand")
        # These tests deliberately use fixed historical clocks. Bind the
        # store's transactional audit clock to the command's own clock.
        store._wall_clock = lambda: command.started_at  # noqa: SLF001
        queued, created = store.admit_preparation(
            RecoveryPreparationCommand(
                binding=RecoveryRunBinding(
                    run_id=command.binding.run_id,
                    actor_id=command.binding.actor_id,
                ),
                admitted_at=command.started_at,
                counts=command.counts,
                attempt_budget_ms=command.attempt_budget_ms,
            )
        )
        assert created is True
        claim = store.claim_next_dispatch(
            stage=RecoveryDispatchStage.PREPARATION,
            worker_id="test-preparation-worker",
            claimed_at=command.started_at,
            claim_expires_at=command.started_at
            + timedelta(milliseconds=command.attempt_budget_ms),
        )
        assert claim is not None
        store.mark_preparing(
            run_id=queued.run_id,
            attempt_id=queued.attempt_id,
            epoch=queued.epoch,
            claim_token=claim.claim_token,
            at=command.started_at,
        )
        prepared = store.complete_preparation(
            run_id=queued.run_id,
            attempt_id=queued.attempt_id,
            epoch=queued.epoch,
            claim_token=claim.claim_token,
            completed_at=command.started_at,
            result=RecoveryPreparedResult(
                manifest_ref=command.binding.manifest_ref,
                preflight_hash=command.binding.preflight_hash,
                snapshot_fingerprint=f"sha256:{queued.run_id}",
                prepared_at=command.started_at,
                expires_at=command.started_at + timedelta(seconds=300),
                counts=queued.counts,
            ),
        )
        assert prepared.binding.manifest_ref == command.binding.manifest_ref
        return prepared

    return admit
