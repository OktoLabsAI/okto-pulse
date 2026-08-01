from __future__ import annotations

import logging
import os
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


# =====================================================================
# Isolamento do registry de runtime values entre testes
# =====================================================================
#
# ``_active_runtime_values`` (okto-pulse-core/src/okto_pulse/core/
# runtime_context.py:196) e um ContextVar que guarda um
# ``_RuntimeValueBinding`` frozen (runtime_context.py:190-193) que APONTA
# para um ``RuntimeValueRegistry`` mutavel.  Duas familias de chamada
# alcancam esse estado com visibilidade DIFERENTE:
#
#   canal A  ContextVar.set()               -> so o Context atual
#            (_current_runtime_binding(create=True),
#             reset_runtime_values() SEM chaves,
#             restore_runtime_values_for_tests, runtime_value_scope)
#   canal B  registry.register()/.discard() -> muta o OBJETO apontado,
#            visivel em TODO Context que copiou a referencia
#
# ``asyncio.run(...)``, testes async do pytest-asyncio e qualquer Task
# rodam em ``contextvars.copy_context()``: a copia recebe o MESMO objeto
# binding com um slot proprio.  Logo B atravessa nos dois sentidos e A nao
# atravessa em nenhum.  A fixture antiga isolava SO pelo canal A, entao nao
# via, nao continha e nao desfazia nada vindo do canal B -- e
# ``RuntimeValueRegistry.copy()`` (runtime_context.py:84-96) carregava o
# MESMO AsyncEngine de teste em teste (so quem implementa
# ``clone_for_runtime`` e clonado), o vetor de "Event loop is closed".
#
# Solucao: UM binding para a sessao inteira (canal A uma unica vez) e todo
# o trabalho por teste no canal B, que e simetrico.

from okto_pulse.core.runtime_context import (  # noqa: E402
    RuntimeValueRegistry,
    runtime_value_scope,
)

#: Registry unico da sessao.  A IDENTIDADE nunca muda.
_SESSION_RUNTIME_VALUES = RuntimeValueRegistry()

# Mundo DETERMINISTICO de listeners semanticos: em producao,
# build_community_session_factory SEMPRE instala os Session-listeners de
# versionamento semantico (processo-global, once). Sem esta linha a suite
# bifurca em dois mundos — testes que criam async_sessionmaker direto rodam
# SEM listeners (verdes por vacuidade) ate que qualquer teste anterior os
# instale, e ai o bridge falha closed (semantic_subject_mutation_actor_required)
# ou constantes calibradas no mundo vazio divergem (budget de statements,
# currentness). Instalar aqui fixa o mundo de producao para TODA ordem.
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import (  # noqa: E402
    install_policy_subject_versioning,
)

install_policy_subject_versioning()

#: Vira ERRO em vez de silencio quando um teste deixa chaves para tras.
#: Rode com OKTO_PULSE_TEST_STRICT_RUNTIME_LEAKS=1 para cacar contaminacao
#: nova; mantenha desligado no default ate o backlog de conversao dos
#: testes com asyncio.run zerar.
_STRICT_RUNTIME_LEAKS = os.environ.get(
    "OKTO_PULSE_TEST_STRICT_RUNTIME_LEAKS", ""
) == "1"


class _ContractTestRequirementLintHook:
    """Hook de lint deterministico usado por toda a suite de contrato."""

    async def stage_requirement_lint(self, context, command):  # noqa: ANN001
        from okto_pulse.core.ports.requirement_lint import (
            RequirementLintWriteResult,
        )

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


def _stage_runtime_value_baseline(registry: RuntimeValueRegistry) -> None:
    """Deixa o registry EXATAMENTE no baseline deterministico da suite.

    A limpeza e uma sequencia de ``discard`` (canal B) no MESMO objeto, logo
    e visivel dentro de qualquer ``asyncio.run`` que o teste abrir -- ao
    contrario de ``reset_runtime_values()`` sem chaves, que troca o binding
    e some (e que, sem binding, faz early-return MUDO).
    """

    from okto_pulse.core import runtime_registry as _runtime_registry
    from okto_pulse.core.infra.config import configure_settings
    from okto_pulse.core.ports.requirement_lint import (
        register_requirement_lint_writer_hook,
    )
    from okto_pulse.community.adapters.sqlalchemy_database import (
        configure_community_database,
    )
    from okto_pulse.community.config import CommunitySettings

    registry.discard(*registry.snapshot())

    # R01C REPLAN-IMP4: o seam de schema-lifecycle e process-global e o
    # composition root da Community o registra.  O baseline e o estado NAO
    # registrado; um teste que precise do orchestrator registra no proprio
    # corpo.  Idem para ports.relational_application.adapter: nada de herdar
    # o registro one-shot do import de community/main.py.
    _runtime_registry.register_relational_runtime_factory(
        lambda url, echo=False: configure_community_database(url, echo=echo)
    )
    register_requirement_lint_writer_hook(_ContractTestRequirementLintHook())
    configure_settings(CommunitySettings())


@pytest.fixture(scope="session", autouse=True)
def _session_runtime_value_binding():
    """Liga UM registry para a sessao inteira -- canal A usado uma so vez.

    ``runtime_value_scope`` e a porta publica baseada em token.  Setup e
    teardown de fixture SINCRONA rodam no Context do proprio pytest, entao
    ``ContextVar.reset`` nunca cruza fronteira de contexto.  NUNCA converter
    esta fixture para async.
    """

    with runtime_value_scope(_SESSION_RUNTIME_VALUES) as registry:
        yield registry


@pytest.fixture(autouse=True)
def _reset_relational_schema_lifecycle_seam(_session_runtime_value_binding):
    """Isola o registry de runtime values por teste, IN-PLACE.

    Invariantes:
      1. o binding NUNCA muda -> toda copia de contexto (asyncio.run, Task,
         contexto de teardown de fixture async) enxerga o MESMO registry;
      2. o registry entra e sai de cada teste no MESMO baseline;
      3. nenhum objeto criado por um teste (engine, sessionmaker, adapter)
         sobrevive ao teardown.
    """

    registry = _session_runtime_value_binding
    _stage_runtime_value_baseline(registry)
    baseline = dict(registry.snapshot())
    try:
        yield
    finally:
        current = dict(registry.snapshot())
        leaked = sorted(
            key
            for key, value in current.items()
            if key not in baseline or baseline[key] is not value
        )
        _stage_runtime_value_baseline(registry)
        if leaked and _STRICT_RUNTIME_LEAKS:
            pytest.fail(
                "runtime-value leak: o teste deixou chaves fora do baseline "
                f"{leaked}. Registre dentro do proprio teste e limpe, ou use "
                "runtime_value_scope(RuntimeValueRegistry()) no corpo.",
                pytrace=False,
            )


@pytest_asyncio.fixture(autouse=True, loop_scope="function")
async def _close_test_community_database_runtime(
    _reset_relational_schema_lifecycle_seam,
):
    """Dispoe um runtime Community criado PELO TESTE antes do loop fechar.

    O registry e o mesmo objeto que o teste mutou (canal B), entao o que
    aparece aqui foi necessariamente criado dentro deste teste -- o baseline
    encenado no setup nao contem runtime relacional algum.
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
    if not isinstance(runtime, CommunityDatabaseRuntime):
        return
    try:
        await runtime.close()
    except RuntimeError as exc:  # pragma: no cover - caminho de defeito
        if "Event loop is closed" not in str(exc):
            raise
        # O engine foi ligado a um loop que o PROPRIO teste ja fechou
        # (padrao asyncio.run em teste sincrono).  As conexoes morreram com
        # o loop; nao existe close correto aqui e falhar o teardown so
        # mascararia o teste real.  O conserto e no teste contaminador.
        logging.getLogger(__name__).warning(
            "community.test.runtime_close_skipped reason=dead_event_loop "
            "runtime=%r",
            runtime,
        )


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
