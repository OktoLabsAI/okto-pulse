"""Community FastAPI application factory."""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import TYPE_CHECKING, AsyncGenerator, Callable, Optional

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from okto_pulse.core.application.startup import (
    apply_persisted_runtime_settings,
    backfill_qa_answered_at,
    compute_tick_catch_up_next_run,
    emit_daily_tick,
    run_startup_schema_sweep,
)
from okto_pulse.core.application.kg_runtime_access import resolve_graph_lifecycle
from okto_pulse.core import configure_auth
from okto_pulse.core import configure_settings
from okto_pulse.community.adapters.sqlalchemy_database import (
    close_db,
    get_engine,
    get_session_factory,
    init_db,
    is_database_runtime_configured,
)
from okto_pulse.community.adapters.sprint_origin_integrity import (
    inspect_sprint_origin_integrity,
)
from okto_pulse.core import StorageProvider, configure_storage
from okto_pulse.core.composition import (
    STRICT_RUNTIME_REQUIRED_PROVIDERS,
    CompositionRuntime,
    RuntimeComposition,
    RuntimeProviderMissing,
    runtime_composition_scope,
    validate_required_providers,
)
from okto_pulse.community.api import api_router
from okto_pulse.community.config import CommunitySettings
from okto_pulse.core.telemetry.http_policy import safe_route_template, should_count_http
from okto_pulse.core.telemetry.telemetry_port_registry import get_telemetry_port
from okto_pulse.core.ports.scheduler import (
    JobSpec,
    KG_DAILY_TICK_JOB_ID,
    SchedulerControl,
    SchedulerResult,
)
from okto_pulse.core.ports.authentication import AuthenticationPort

if TYPE_CHECKING:
    from okto_pulse.core.ports.runtime_workers import RuntimeWorkerRegistry

logger = logging.getLogger(__name__)


class _RuntimeCompositionASGIMiddleware:
    """Bind the app-owned composition to each ASGI task without global writes."""

    def __init__(self, app, composition: RuntimeComposition) -> None:
        self.app = app
        self.composition = composition

    async def __call__(self, scope, receive, send) -> None:
        task = asyncio.current_task()
        previous_name = task.get_name() if task is not None else None
        if task is not None and scope.get("type") == "http":
            task.set_name(
                f"community.api.{scope.get('method', 'UNKNOWN')}:"
                f"{scope.get('path', '')[:180]}"
            )
        try:
            with runtime_composition_scope(self.composition):
                await self.app(scope, receive, send)
        finally:
            if task is not None and previous_name is not None:
                task.set_name(previous_name)


class _TelemetryASGIMiddleware:
    """Telemetria HTTP como ASGI puro (sem BaseHTTPMiddleware).

    Intercepta ``http.response.start`` para capturar o status e registra o
    evento quando o downstream conclui (para streams, ao fim do stream —
    mesma semântica da versão BaseHTTPMiddleware). Não cria task group nem
    cancel scope em volta da resposta, o que mantém o caminho de
    cancelamento de SSE/streaming idêntico ao do servidor puro.
    """

    def __init__(self, app, settings: CommunitySettings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope, receive, send) -> None:
        # R5A-C: explicit, testable HTTP telemetry policy (counts /api + /mcp; excludes
        # /health /docs /openapi.json /redoc; no lookalike false positives) replaces
        # the old implicit startswith('/api/') filter.
        if scope["type"] != "http" or not should_count_http(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status_holder = {"status": 500}
        error_class = None

        async def send_wrapper(message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            error_class = exc.__class__.__name__
            raise
        finally:
            # O router popula scope["route"] durante o dispatch — disponível
            # aqui depois que o downstream rodou. safe_route_template prefere o
            # PADRÃO da rota resolvida (placeholders) e nunca emite path concreto.
            route = scope.get("route")
            route_template = safe_route_template(route, scope.get("path", ""))
            payload = {
                "method": scope.get("method", ""),
                "route_template": route_template,
                "status_code": status_holder["status"],
                "duration_ms": int((time.perf_counter() - started) * 1000),
            }
            if error_class:
                payload["error_class"] = error_class
            try:
                get_telemetry_port(self.settings).record_event("http", payload)
            except Exception:
                logger.debug("telemetry.record_failed", exc_info=True)


async def shutdown_kg_then_db(
    close_db: Callable[[], "object"],
    *,
    logger: logging.Logger,
    graph_lifecycle_provider: Callable[[], object] | None = None,
    run_blocking: Callable[[Callable[[], object]], object] | None = None,
) -> None:
    """Release KG graph connections, then close the DB (R16-D, tr_5c727d9b).

    Closes the board graphs via the spec #06 ``GraphLifecycle`` port
    (``graph_lifecycle.close(None)`` replaces ``kg.schema.close_all_connections``),
    driven OFF the event loop because the embedded adapter is synchronous under
    the async port (the close drains readers up to ~5s/board and would freeze
    shutdown if run on the loop).

    A KG-close (or registry-resolution) failure is logged as
    ``kg.shutdown.close_connections_failed`` and NEVER blocks the DB close —
    ``close_db()`` ALWAYS runs afterwards. ``graph_lifecycle_provider`` /
    ``run_blocking`` are injectable for deterministic tests.
    """
    try:
        if graph_lifecycle_provider is None:
            graph_lifecycle = resolve_graph_lifecycle()
        else:
            graph_lifecycle = graph_lifecycle_provider()
        if run_blocking is None:
            await asyncio.to_thread(lambda: asyncio.run(graph_lifecycle.close(None)))
        else:
            await run_blocking(lambda: graph_lifecycle.close(None))
    except Exception as exc:  # noqa: BLE001 — never block the DB close
        logger.warning(
            "kg.shutdown.close_connections_failed err=%s",
            exc,
            extra={
                "event": "kg.shutdown.close_connections_failed",
                "error": str(exc),
            },
        )
    await close_db()


def build_kg_daily_tick_job_spec(
    interval_minutes: int,
    *,
    next_run_time: datetime | None = None,
) -> JobSpec:
    """Build the core-owned policy for the KG decay scheduler job."""

    return JobSpec(
        job_id=KG_DAILY_TICK_JOB_ID,
        interval_minutes=int(interval_minutes),
        max_instances=1,
        coalesce=True,
        replace_existing=True,
        next_run_time=next_run_time,
        handler_ref="okto_pulse.core.application.startup.emit_daily_tick",
    )


async def register_kg_daily_tick_job(
    scheduler_control: SchedulerControl | None,
    *,
    logger: logging.Logger = logger,
) -> SchedulerResult | None:
    """Register the KG daily tick through the scheduler port.

    Core owns the JobSpec policy and the catch-up calculation. Edition adapters
    own how that specification is mapped to their concrete scheduler runtime.
    """

    if os.getenv("KG_DAILY_TICK_DISABLED") == "1":
        return None

    from okto_pulse.core import get_settings as _get_settings

    interval_minutes = _get_settings().kg_decay_tick_interval_minutes
    next_run_time = None
    try:
        next_run_time = await compute_tick_catch_up_next_run(interval_minutes)
    except Exception as exc:
        logger.warning(
            "kg.tick.catch_up_compute_failed err=%s",
            exc,
            extra={"event": "kg.tick.catch_up_compute_failed"},
        )

    job_spec = build_kg_daily_tick_job_spec(
        interval_minutes,
        next_run_time=next_run_time,
    )
    if scheduler_control is None:
        logger.info(
            "kg.tick.scheduler_skipped reason=no_provider interval_minutes=%d",
            interval_minutes,
            extra={
                "event": "kg.tick.scheduler_skipped",
                "reason": "no_provider",
                "interval_minutes": interval_minutes,
            },
        )
        return SchedulerResult(
            job_id=job_spec.job_id,
            scheduled=False,
            message="no_provider",
            audit_status="skipped",
        )

    try:
        result = await scheduler_control.register_job(job_spec, emit_daily_tick)
    except Exception as exc:
        logger.warning(
            "kg.tick.scheduler_failed err=%s",
            exc,
            extra={"event": "kg.tick.scheduler_failed"},
        )
        return SchedulerResult(
            job_id=job_spec.job_id,
            scheduled=False,
            message=str(exc),
            audit_status="failed",
        )

    if result.scheduled:
        logger.info(
            "kg.tick.scheduler_started interval_minutes=%d",
            interval_minutes,
            extra={
                "event": "kg.tick.scheduler_started",
                "interval_minutes": interval_minutes,
            },
        )
    return result


def create_app(
    settings: CommunitySettings,
    auth_provider: AuthenticationPort,
    storage_provider: StorageProvider,
    *,
    cors_origins: list[str] | None = None,
    lifespan: Optional[Callable] = None,
    composition: RuntimeComposition | None = None,
    strict_runtime: bool = False,
    runtime_shell_only: bool = False,
    runtime_worker_registry: Optional["RuntimeWorkerRegistry"] = None,
    edition_routers: tuple[APIRouter, ...] = (),
) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        settings: Materialized Community edition settings.
        auth_provider: Authentication provider implementation
        storage_provider: File storage provider implementation
        cors_origins: List of allowed CORS origins
        composition: Optional explicit :class:`RuntimeComposition` (spec #03). When
            omitted, the historical wiring is preserved (backward compatible).
        strict_runtime: When True (spec #03, tr_f9d2f84f) a #03-owned provider that
            is absent fails fast with ``runtime_provider_missing`` — no silent
            fallback to a concrete default. The KG registry stays deferred to #05.
        runtime_shell_only: Explicit opt-in shell mode for strict/core-only smoke
            paths. When enabled, ``create_app`` does not open the default database
            engine; callers must provide a strict ``RuntimeComposition`` with
            lifecycle hooks. The productive default remains unchanged.
        runtime_worker_registry: Optional explicit runtime-worker registry. The
            core default is ``None``; edition composition roots own worker
            start/stop decisions and may inject a registry when appropriate.
    """
    if auth_provider is None:
        raise TypeError("auth_provider is required")
    if storage_provider is None:
        raise TypeError("storage_provider is required")

    # spec #03 (tr_f9d2f84f): strict_runtime requires an explicit composition and
    # rejects any missing required owned provider deterministically. Default
    # (strict_runtime=False) keeps the historical behaviour intact.
    if strict_runtime:
        if composition is None:
            raise RuntimeProviderMissing(
                "composition", missing=list(STRICT_RUNTIME_REQUIRED_PROVIDERS)
            )
        validate_required_providers(
            composition,
            require_lifecycle_hooks=lifespan is None,
        )
    if runtime_shell_only and not strict_runtime:
        raise ValueError("runtime_shell_only requires strict_runtime=True")

    if composition is not None:
        if runtime_worker_registry is None:
            runtime_worker_registry = composition.worker_registry

    # Legacy callers still receive process-level registration. A composed app
    # never mutates these globals: its ASGI scope resolves providers directly
    # from the immutable RuntimeComposition.
    if composition is None:
        configure_settings(settings)
        configure_auth(auth_provider)
        configure_storage(storage_provider)

    # The concrete relational runtime is edition-owned. Community/SaaS adapters
    # must build the engine/session and inject them before the default lifespan
    # reaches init_db; the core app factory never opens a database itself.
    if not runtime_shell_only and not is_database_runtime_configured():
        raise RuntimeProviderMissing(
            "session_factory",
            missing=["relational_runtime"],
        )

    shell_lifespan = None
    if runtime_shell_only and lifespan is None and composition is not None:
        shell_runtime = CompositionRuntime(composition, strict_runtime=strict_runtime)

        @asynccontextmanager
        async def _shell_lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
            async with shell_runtime.app_lifespan():
                yield

        shell_lifespan = _shell_lifespan

    @asynccontextmanager
    async def _default_lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        await init_db()

        # Apply persisted runtime settings BEFORE any module opens a Kùzu
        # Database instance. The graph adapter reads configured edition settings,
        # so we just need configure_settings() to be updated by then.
        try:
            await apply_persisted_runtime_settings()
        except Exception:
            # Fresh install, table may not exist yet — that's fine, defaults
            # already cover the safe budget.
            pass

        # Self-heal: Q&A respondidas herdadas sem answered_at viravam
        # falso-abertas no badge open_qa_count (a herança não copiava o
        # timestamp). Idempotente — só carimba linhas respondidas órfãs.
        try:
            factory = get_session_factory()
            async with factory() as _qa_session:
                _qa_fixed = await backfill_qa_answered_at(_qa_session)
            if _qa_fixed:
                logger.info(
                    "qa.answered_at.backfilled %s",
                    _qa_fixed,
                    extra={
                        "event": "qa.answered_at.backfilled",
                        "fixed": _qa_fixed,
                    },
                )
        except Exception as _exc:
            logger.warning(
                "qa.answered_at.backfill_failed err=%s",
                _exc,
                extra={"event": "qa.answered_at.backfill_failed"},
            )

        # Self-heal AFG (investigacao 2026-06-10): findings de arquitetura
        # so nasciam em saves pos-feature; 83% dos designs nunca tiveram
        # run e o gate avaliava tabela vazia. O sweep re-avalia os designs
        # (payloads re-hidratados) em BACKGROUND para nao atrasar o boot.
        async def _afg_backfill_task() -> None:
            try:
                from okto_pulse.core.services.architecture import (
                    backfill_architecture_finding_runs,
                )

                factory = get_session_factory()
                async with factory() as _afg_session:
                    _afg_stats = await backfill_architecture_finding_runs(
                        _afg_session,
                        only_missing=True,
                    )
                logger.info(
                    "architecture.finding_backfill.completed %s",
                    _afg_stats,
                    extra={
                        "event": "architecture.finding_backfill.completed",
                        **_afg_stats,
                    },
                )
            except Exception as _afg_exc:
                logger.warning(
                    "architecture.finding_backfill.failed err=%s",
                    _afg_exc,
                    extra={"event": "architecture.finding_backfill.failed"},
                )

        _afg_task = asyncio.create_task(_afg_backfill_task())

        # R08C: core.app no longer constructs or starts local runtime workers by
        # default. Edition composition roots own concrete worker decisions and
        # may inject an explicit registry through the public runtime contract.
        if runtime_worker_registry is not None:
            await runtime_worker_registry.start_all()
            for failure in runtime_worker_registry.start_failures:
                _event = {
                    "consolidation_worker": ("kg.consolidation_worker.start_failed"),
                    "cognitive_closeout_worker": (
                        "kg.cognitive_closeout_worker.start_failed"
                    ),
                    "outbox_worker": "kg.outbox_worker.start_failed",
                }.get(failure.family, "kg.worker.start_failed")
                logger.warning(
                    "%s err=%s",
                    _event,
                    failure.message,
                    extra={"event": _event, "error": failure.message},
                )

        # NC-10 fix: migrate per-board KG schemas idempotently on boot.
        # Boards created before SCHEMA_VERSION 0.3.3 lack the
        # ``last_recomputed_at`` column on every node type, which floods
        # the daily tick with ``Cannot find property last_recomputed_at``
        # warnings and silently skips those boards' decay recompute.
        # ``apply_schema_to_connection`` is idempotent (CREATE NODE TABLE
        # IF NOT EXISTS, ALTER TABLE ADD COLUMN IF NOT EXISTS) so this is
        # safe to run on every startup; soft-fail per board so a single
        # broken Kùzu file does not block the app from booting.
        try:
            await run_startup_schema_sweep(
                uow_factory=composition.uow_factory,
                logger=logger,
            )
        except Exception as _exc:
            # Tabela ainda não existe em fresh install ou Kùzu não
            # instalado — não bloqueia boot.
            logger.debug(
                "kg.schema.migration_skipped err=%s",
                _exc,
                extra={"event": "kg.schema.migration_skipped"},
            )
        # Core owns the KG decay job policy through JobSpec; editions own the
        # concrete scheduler runtime and inject it as SchedulerControl.
        scheduler_control = (
            getattr(composition, "scheduler_control", None)
            if composition is not None
            else None
        )
        await register_kg_daily_tick_job(
            scheduler_control,
            logger=logger,
        )
        try:
            yield
        finally:
            # Reverse order: stop dispatcher first so in-flight handlers
            # finish before the downstream workers they depend on exit.
            if scheduler_control is not None:
                try:
                    await scheduler_control.shutdown(wait=False)
                except Exception:
                    pass
            worker_stop_failures = ()
            if runtime_worker_registry is not None:
                worker_stop_failures = await runtime_worker_registry.stop_all()
                for failure in worker_stop_failures:
                    logger.warning(
                        "kg.worker.stop_failed family=%s err=%s",
                        failure.family,
                        failure.message,
                        extra={
                            "event": "kg.worker.stop_failed",
                            "family": failure.family,
                            "error": failure.message,
                        },
                    )
            native_drain_incomplete = any(
                failure.resource_close_unsafe
                for failure in worker_stop_failures
            )
            if native_drain_incomplete:
                blocked_families = sorted(
                    failure.family
                    for failure in worker_stop_failures
                    if failure.resource_close_unsafe
                )
                logger.critical(
                    "community.shutdown.native_drain_incomplete "
                    "families=%s graph_close_skipped=true db_close_skipped=true",
                    ",".join(blocked_families),
                    extra={
                        "event": "community.shutdown.native_drain_incomplete",
                        "families": blocked_families,
                        "graph_close_skipped": True,
                        "db_close_skipped": True,
                    },
                )
            # Release LadybugDB handles explicitly on graceful shutdown.
            # Relying on interpreter teardown can leave WAL sidecars as the
            # only holder of recent writes; a later bootstrap probe may then
            # see a corrupt WAL and previously attempted an automatic purge.
            #
            # R16-D: extracted to shutdown_kg_then_db (tr_5c727d9b) — closes via
            # the spec #06 GraphLifecycle port off the loop; a KG-close failure
            # is logged (kg.shutdown.close_connections_failed) and never blocks
            # the DB close (close_db always runs).
            if not native_drain_incomplete:
                await shutdown_kg_then_db(close_db, logger=logger)

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan if lifespan else (shell_lifespan or _default_lifespan),
    )

    # R-P2-06B: preserve the composition so request handlers can resolve
    # composition-owned providers (e.g. the SchedulerControl that applies the
    # runtime settings tick effect) WITHOUT a process-global singleton fallback.
    # May be ``None`` for legacy/non-composed callers — handlers then treat the
    # absent provider as an explicit skip.
    app.state.runtime_composition = composition

    # CORS
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # Root-cause fix (2026-06-09): este middleware era um
    # ``@app.middleware("http")`` (= BaseHTTPMiddleware). BaseHTTPMiddleware
    # consome respostas de streaming dentro de um task group do anyio; quando
    # o cliente de um SSE desconectava, o cancel scope cancelava o generator
    # com hard-cancel no meio de awaits de DB — vazando conexões do pool
    # (exaustão → "travamento"). Como ASGI puro, a desconexão fecha o
    # generator pelo caminho normal (aclose), sem cancel scope atravessando
    # o cleanup.
    app.add_middleware(_TelemetryASGIMiddleware, settings=settings)
    if composition is not None:
        app.add_middleware(
            _RuntimeCompositionASGIMiddleware,
            composition=composition,
        )

    # Health check
    @app.get("/health")
    async def health_check():
        # Liveness must remain constant-time and independent of database size
        # or availability.  Storage-backed diagnostics live on the explicit
        # endpoint below so an orchestrator probe cannot trigger a full scan.
        return {
            "status": "healthy",
            "version": settings.app_version,
        }

    @app.get("/health/integrity")
    async def health_integrity_check():
        sprint_origin_finding = await inspect_sprint_origin_integrity(get_engine)
        return {
            # Diagnostics remain HTTP 200 so operators can inspect a complete
            # finding even when relational integrity is degraded or critical.
            "status": "healthy",
            "version": settings.app_version,
            "integrity_status": sprint_origin_finding["status"],
            "findings": {
                "sprint_origin_integrity": sprint_origin_finding,
            },
        }

    # MockupDesignSystemGate (spec 3a006f65 / card 0192f58d): the gate runs inside the
    # service-layer entity update methods, so a bulk screen_mockups REST update that
    # violates a blocking Design System gate raises DesignSystemError. Translate it to a
    # clean, actionable structured response (FR8) on every REST surface in one place.
    from fastapi.responses import JSONResponse

    from okto_pulse.core.services.design_system import DesignSystemError

    @app.exception_handler(DesignSystemError)
    async def _design_system_error_handler(_request, exc: DesignSystemError):
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    # S-LANE-01: canonicalize an invalid sprint ``lane_type`` (which fails Pydantic
    # body validation BEFORE the route handler runs) into the shared envelope.
    # Extracted to a module-level installer so tests register the identical handler.
    install_request_validation_handler(app)

    # API routes
    app.include_router(api_router)
    for edition_router in edition_routers:
        app.include_router(edition_router)

    return app


def install_request_validation_handler(app: FastAPI) -> None:
    """Register the canonical RequestValidationError handler (card S-LANE-01).

    An invalid sprint ``lane_type`` fails Pydantic body validation BEFORE the
    route handler runs (FastAPI raises ``RequestValidationError``), so without
    this the agent/UI would see the raw Pydantic surface (the
    ``errors.pydantic.dev`` URL, internal locs). This canonicalizes that one
    mapped enum field into the shared envelope (status 422,
    ``mutation_applied=false`` — the boundary rejects before any service call, so
    nothing persists) and delegates every other validation error to FastAPI's
    default handler, leaving unmapped fields unaffected. The same envelope is
    produced on the MCP surface.
    """
    from fastapi.exception_handlers import request_validation_exception_handler
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse

    from okto_pulse.core.inbound.enum_error_envelope import canonical_enum_error

    @app.exception_handler(RequestValidationError)
    async def _request_validation_error_handler(request, exc: RequestValidationError):
        envelope = canonical_enum_error(exc.errors())
        if envelope is not None:
            return JSONResponse(status_code=422, content=envelope)
        return await request_validation_exception_handler(request, exc)
