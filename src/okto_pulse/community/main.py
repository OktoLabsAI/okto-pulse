"""Community edition application entry point."""

# ruff: noqa: E402

import warnings
warnings.filterwarnings(
    "ignore",
    message=r"urllib3.*or chardet.*doesn't match a supported version",
    category=Warning,
)

import asyncio
import contextlib
import logging
import math
import os
import time
from datetime import timezone
from pathlib import Path
from typing import AsyncGenerator

import uvicorn
from fastapi.staticfiles import StaticFiles
from sqlalchemy import event

from okto_pulse.core.app import create_app
from okto_pulse.core.infra.config import get_settings
from okto_pulse.core.infra.database import get_engine, get_session_factory, init_db, close_db
from okto_pulse.core.infra.storage import FileSystemStorageProvider
from okto_pulse.core.kg.embedding import SentenceTransformerProvider, StubEmbeddingProvider
from okto_pulse.core.kg.interfaces.registry import configure_kg_registry, get_kg_registry
# NOTE: MCP server is imported lazily inside create_community_app (after
# create_app has called configure_settings) and inside combined_lifespan
# (after init_db). Module-level import would cache the default settings
# singleton via get_settings() at import time and break runtime config.
# Settings cache trap respected by Spec 23350275 (Fix C, BR5).
from okto_pulse.community.auth import LocalAuthProvider
from okto_pulse.community.config import CommunitySettings
from okto_pulse.community.runtime import (
    build_uvicorn_log_config,
    run_async_server,
    set_shutdown_log_suppression,
)
from okto_pulse.community.seed import seed_community_defaults

_EMBEDDING_LOGGER = logging.getLogger("okto_pulse.community.embedding")
_STARTUP_LOGGER = logging.getLogger("uvicorn.error")
_DEFAULT_STARTUP_TIMEOUT_SECONDS = 120.0
_DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 5.0

# Preload retry policy: 3 attempts, exponential backoff (2s, 4s, 8s), 30s total budget.
# Only transient network errors retry. ImportError / OSError (disk full) / ValueError
# are deterministic and should fail fast — no amount of retrying will fix them.
_EMBEDDING_PRELOAD_ATTEMPTS = 3
_EMBEDDING_PRELOAD_BACKOFF_S = (2.0, 4.0, 8.0)
_EMBEDDING_PRELOAD_BUDGET_S = 30.0


def _startup_timeout_seconds() -> float:
    """Return the readiness timeout used while uvicorn lifespans complete."""
    raw = (
        os.environ.get("OKTO_PULSE_STARTUP_TIMEOUT_SECONDS")
        or os.environ.get("OKTO_PULSE_STARTUP_TIMEOUT")
    )
    if not raw:
        return _DEFAULT_STARTUP_TIMEOUT_SECONDS
    try:
        timeout = float(raw)
    except ValueError:
        _STARTUP_LOGGER.warning(
            "Invalid OKTO_PULSE_STARTUP_TIMEOUT_SECONDS=%r; using %.0fs.",
            raw,
            _DEFAULT_STARTUP_TIMEOUT_SECONDS,
        )
        return _DEFAULT_STARTUP_TIMEOUT_SECONDS
    if timeout < 1:
        _STARTUP_LOGGER.warning(
            "OKTO_PULSE_STARTUP_TIMEOUT_SECONDS must be >= 1; using %.0fs.",
            _DEFAULT_STARTUP_TIMEOUT_SECONDS,
        )
        return _DEFAULT_STARTUP_TIMEOUT_SECONDS
    return timeout


def _shutdown_timeout_seconds() -> float:
    """Return the graceful shutdown timeout for open HTTP/WebSocket streams."""
    raw = (
        os.environ.get("OKTO_PULSE_SHUTDOWN_TIMEOUT_SECONDS")
        or os.environ.get("OKTO_PULSE_SHUTDOWN_TIMEOUT")
    )
    if not raw:
        return _DEFAULT_SHUTDOWN_TIMEOUT_SECONDS
    try:
        timeout = float(raw)
    except ValueError:
        _STARTUP_LOGGER.warning(
            "Invalid OKTO_PULSE_SHUTDOWN_TIMEOUT_SECONDS=%r; using %.0fs.",
            raw,
            _DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
        )
        return _DEFAULT_SHUTDOWN_TIMEOUT_SECONDS
    if timeout < 1:
        _STARTUP_LOGGER.warning(
            "OKTO_PULSE_SHUTDOWN_TIMEOUT_SECONDS must be >= 1; using %.0fs.",
            _DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
        )
        return _DEFAULT_SHUTDOWN_TIMEOUT_SECONDS
    return timeout


def _is_transient_network_error(exc: BaseException) -> bool:
    """Return True when `exc` is worth retrying (network glitch / timeout)."""
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    # requests is a transitive dep of sentence-transformers / huggingface_hub;
    # import it lazily so the preload logic does not pay the import cost when
    # the provider is already loaded.
    try:
        import requests  # type: ignore
    except ImportError:
        return False
    return isinstance(exc, (requests.ConnectionError, requests.Timeout))


async def _preload_embedding_model(settings: CommunitySettings) -> None:
    """Preload the sentence-transformers model at startup.

    On success emits `kg.embedding.loaded`; on unrecoverable failure swaps
    the registry to a StubEmbeddingProvider and emits `kg.embedding.load_failed`
    so the server keeps serving (semantic search degrades, but app is up).
    """
    registry = get_kg_registry()
    provider = registry.embedding_provider
    if not isinstance(provider, SentenceTransformerProvider):
        # Stub mode or custom provider — nothing to preload.
        return

    model_name = provider.model_name
    started = time.monotonic()

    def _load() -> None:
        provider._get_model()

    last_exc: Exception | None = None
    for attempt in range(1, _EMBEDDING_PRELOAD_ATTEMPTS + 1):
        if time.monotonic() - started >= _EMBEDDING_PRELOAD_BUDGET_S:
            last_exc = last_exc or TimeoutError("embedding preload budget exhausted")
            break
        try:
            _load()
            duration_ms = int((time.monotonic() - started) * 1000)
            _EMBEDDING_LOGGER.info(
                "kg.embedding.loaded",
                extra={
                    "event": "kg.embedding.loaded",
                    "model": model_name,
                    "dimension": provider.dim,
                    "duration_ms": duration_ms,
                    "attempt": attempt,
                },
            )
            return
        except Exception as exc:
            last_exc = exc
            if not _is_transient_network_error(exc):
                # ImportError, OSError, ValueError, etc. — fail fast.
                break
            if attempt < _EMBEDDING_PRELOAD_ATTEMPTS:
                backoff = _EMBEDDING_PRELOAD_BACKOFF_S[attempt - 1]
                remaining = _EMBEDDING_PRELOAD_BUDGET_S - (time.monotonic() - started)
                if remaining <= 0:
                    break
                await asyncio.sleep(min(backoff, remaining))

    registry.embedding_provider = StubEmbeddingProvider(dim=settings.kg_embedding_dim)
    _EMBEDDING_LOGGER.warning(
        "kg.embedding.load_failed",
        extra={
            "event": "kg.embedding.load_failed",
            "model": model_name,
            "error": repr(last_exc) if last_exc else "unknown",
            "fallback": "StubEmbeddingProvider",
        },
    )

# Frontend dist embedded in the package (built with VITE_AUTH_MODE=local)
FRONTEND_DIR = Path(__file__).parent / "frontend_dist"


def _ensure_data_dir(settings: CommunitySettings) -> None:
    """Create data directory structure if it doesn't exist."""
    data_path = Path(settings.data_dir)
    # Use data_dir as base for KG (kg_base_dir was removed from CommunitySettings)
    kg_base = data_path / "kg"
    for subdir in [data_path, data_path / "data", data_path / "uploads", kg_base / "boards"]:
        subdir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(str(data_path), 0o700)
    except (OSError, NotImplementedError):
        pass


def _configure_sqlite_pragmas(engine) -> None:
    """Configure SQLite WAL mode and foreign keys via event listener."""
    @event.listens_for(engine.sync_engine, "connect")
    def set_sqlite_pragmas(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def _mount_frontend(
    app,
    frontend_dir: Path,
    api_port: int = 8100,
    mcp_port: int = 8101,
    public_host: str = "127.0.0.1",
    public_api_port: int | None = None,
    public_mcp_port: int | None = None,
) -> None:
    """Mount the pre-built frontend SPA on the FastAPI app.

    Uses Starlette middleware approach to avoid route-ordering issues:
    - /assets/* → StaticFiles mount (correct MIME types)
    - /api/*, /health, /docs, /openapi.json → pass through to FastAPI
    - Everything else → index.html (SPA routing)

    Injects runtime configuration for API and MCP ports to support custom ports.
    Set PUBLIC_HOST / PUBLIC_API_PORT / PUBLIC_MCP_PORT env vars to override the
    URLs the browser SPA uses (needed when behind a reverse proxy or NAT).
    """
    if not frontend_dir.exists():
        return

    assets_dir = frontend_dir / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="frontend-assets")

    index_html_path = frontend_dir / "index.html"
    if not index_html_path.exists():
        return

    # Read index.html content once for injection
    index_html_content = index_html_path.read_text()

    # Inject config.js script tag before the closing </head> tag
    config_script_tag = '  <script src="/config.js"></script>\n'
    if '</head>' in index_html_content:
        injected_index_html = index_html_content.replace('</head>', config_script_tag + '</head>')
    else:
        injected_index_html = index_html_content

    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import Response

    _API_PREFIXES = ("/api/", "/health", "/docs", "/openapi.json", "/redoc", "/mcp", "/config.js")

    class SPAMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            response = await call_next(request)
            # If the response is 404 and the path is not an API/asset path,
            # serve index.html for SPA client-side routing
            if response.status_code == 404:
                path = request.url.path
                if not any(path.startswith(p) for p in _API_PREFIXES) and not path.startswith("/assets"):
                    return Response(content=injected_index_html, media_type="text/html")
            return response

    # Inject runtime configuration BEFORE SPA middleware.
    # PUBLIC_* env vars override the URLs the browser SPA uses — set them when
    # the server is accessed through a different host/port than the internal bind
    # (e.g. NAT, reverse proxy, or LAN deployment).
    _pub_host = public_host
    _pub_api_port = public_api_port if public_api_port is not None else api_port
    _pub_mcp_port = public_mcp_port if public_mcp_port is not None else mcp_port
    config_script = f"""
// Runtime configuration injected by server
window.OKTO_PULSE_CONFIG = {{
    API_URL: 'http://{_pub_host}:{_pub_api_port}/api/v1',
    MCP_URL: 'http://{_pub_host}:{_pub_mcp_port}'
}};
"""

    # Add a route to inject config (must be BEFORE SPA middleware)
    @app.get("/config.js")
    async def get_config():
        from fastapi.responses import Response
        return Response(content=config_script, media_type="application/javascript")

    app.add_middleware(SPAMiddleware)


def create_community_app():
    """Create the community FastAPI application with embedded frontend."""
    settings = CommunitySettings()

    # Read ports from environment (set by CLI) or use defaults
    api_port = int(os.environ.get("OKTO_PULSE_PORT", str(settings.port)))
    mcp_port = int(os.environ.get("OKTO_PULSE_MCP_PORT", str(settings.mcp_port)))
    # Public-facing host/ports for the browser SPA config.js.
    # Override when the container is accessed through a different host/port
    # than the internal bind address (NAT, reverse proxy, LAN deployment).
    public_host = os.environ.get("PUBLIC_HOST", "127.0.0.1")
    public_api_port_env = os.environ.get("PUBLIC_API_PORT")
    public_mcp_port_env = os.environ.get("PUBLIC_MCP_PORT")
    public_api_port = int(public_api_port_env) if public_api_port_env else None
    public_mcp_port = int(public_mcp_port_env) if public_mcp_port_env else None

    _ensure_data_dir(settings)

    auth = LocalAuthProvider()
    storage = FileSystemStorageProvider(settings.upload_dir)

    # Combined lifespan: seed data, preload embeddings, start KG workers,
    # register the MCP session factory so the mounted sub-app finds the DB.
    async def combined_lifespan(app_instance) -> AsyncGenerator[None, None]:
        await init_db()
        async with get_session_factory()() as db:
            result = await seed_community_defaults(db)
            if result:
                board, agent, api_key = result
                print(f"\n{'='*60}")
                print("  Okto Pulse Community — First Boot Setup")
                print(f"{'='*60}")
                print(f"  Board created: {board.name} ({board.id})")
                print(f"  Agent created: {agent.name}")
                print(f"  API Key: {api_key}")
                print(f"  MCP URL: http://localhost:{mcp_port}/mcp?api_key={api_key}")
                print(f"{'='*60}\n")

        # Preload the embedding model before serving requests so the first
        # KG search doesn't pay the multi-second model-load cost synchronously.
        await _preload_embedding_model(settings)

        # Start the KG background workers
        from okto_pulse.core.events.dispatcher import EventDispatcher, set_dispatcher
        from okto_pulse.core.kg.workers.consolidation import ConsolidationWorker
        from okto_pulse.core.kg.workers.cleanup import get_cleanup_worker
        from okto_pulse.core.kg.global_discovery.outbox_worker import OutboxWorker
        from okto_pulse.core.services.settings_service import apply_persisted_settings_to_core_settings

        await apply_persisted_settings_to_core_settings()

        event_dispatcher = EventDispatcher(get_session_factory())
        await event_dispatcher.start()
        set_dispatcher(event_dispatcher)

        cleanup_worker = None
        consolidation_worker = None
        outbox_worker = None
        scheduler = None

        kg_settings = get_settings()
        if getattr(kg_settings, "kg_cleanup_enabled", True):
            # Singleton picks interval_seconds from settings — passing
            # a session_factory positionally would shadow that field
            # and crash asyncio.sleep() with a TypeError.
            cleanup_worker = get_cleanup_worker()
            await cleanup_worker.start()

        consolidation_worker = ConsolidationWorker(get_session_factory())
        await consolidation_worker.start()

        outbox_worker = OutboxWorker(get_session_factory())
        await outbox_worker.start()

        # Daily decay tick scheduler (Ideação #4 IMPL-D, dec_bc0eaeec).
        # Honor KG_DAILY_TICK_DISABLED for tests; soft-fail if APScheduler
        # is unavailable (community ships it, but the catch keeps boot
        # resilient if the wheel was stripped down).
        if os.getenv("KG_DAILY_TICK_DISABLED") != "1":
            try:
                from apscheduler.schedulers.asyncio import AsyncIOScheduler
                from apscheduler.triggers.interval import IntervalTrigger
                from okto_pulse.core.app import _emit_daily_tick
                from okto_pulse.core.infra.config import get_settings as _get_settings
                from okto_pulse.core.kg.scheduler_singleton import set_scheduler

                _interval_minutes = _get_settings().kg_decay_tick_interval_minutes
                scheduler = AsyncIOScheduler(timezone=timezone.utc)
                scheduler.add_job(
                    _emit_daily_tick,
                    # Spec 54399628 (Wave 2 NC f9732afc) — IntervalTrigger
                    # honra setting persistido + hot-reload via singleton.
                    trigger=IntervalTrigger(
                        minutes=_interval_minutes,
                        timezone=timezone.utc,
                    ),
                    id="kg_daily_tick",
                    replace_existing=True,
                    max_instances=1,
                    coalesce=True,
                )
                scheduler.start()
                set_scheduler(scheduler)
            except Exception:
                scheduler = None

        # Spec 23350275 (Fix C): the MCP sub-app shares this process, this
        # FastAPI app, and this database. Register the session factory now
        # — the mount happens once below in create_community_app so the
        # routing table is finalized before uvicorn starts serving.
        # Lazy import preserves the settings cache trap: configure_settings
        # has already run via create_app().
        from okto_pulse.core.mcp.server import register_session_factory
        register_session_factory(get_session_factory())

        yield

        if scheduler is not None:
            try:
                scheduler.shutdown(wait=False)
            except Exception:
                pass
        await event_dispatcher.stop(timeout=5.0)
        set_dispatcher(None)
        if outbox_worker:
            await outbox_worker.stop()
        if cleanup_worker:
            await cleanup_worker.stop()
        if consolidation_worker:
            await consolidation_worker.stop()
        await close_db()

    app = create_app(
        settings=settings,
        auth_provider=auth,
        storage_provider=storage,
        cors_origins=settings.cors_origins_list,
        lifespan=combined_lifespan,
    )

    # Configure SQLite pragmas AFTER create_database was called by create_app
    _configure_sqlite_pragmas(get_engine())

    # Bootstrap the KG provider registry with all embedded providers.
    # session_factory auto-wires audit_repo + event_bus.
    configure_kg_registry(session_factory=get_session_factory())

    # System flags endpoint — used by the frontend to honor CLI/env terms pre-acceptance.
    from okto_pulse.community.acceptance import acceptance_status

    @app.get("/api/v1/me/system-flags")
    def get_system_flags():
        """Surface env/CLI-driven flags the SPA needs at boot time."""
        return {"terms_acceptance": acceptance_status()}

    # Mount frontend (must be AFTER API routes so /api/v1/* takes precedence)
    _mount_frontend(
        app, FRONTEND_DIR,
        api_port=api_port, mcp_port=mcp_port,
        public_host=public_host,
        public_api_port=public_api_port,
        public_mcp_port=public_mcp_port,
    )

    return app


# Module-level app created on import — uvicorn needs "module:app" reference.
# The print was moved to cmd_serve in cli.py to avoid showing wrong port.
app = create_community_app()


async def _wait_for_server_started(
    server_name: str,
    server: uvicorn.Server,
    task: asyncio.Task[None],
    timeout_seconds: float | None = None,
) -> None:
    if timeout_seconds is None:
        timeout_seconds = _startup_timeout_seconds()
    deadline = time.monotonic() + timeout_seconds
    while not server.started:
        if task.done():
            await task
            raise RuntimeError(f"{server_name} server stopped before startup completed.")
        if time.monotonic() >= deadline:
            server.should_exit = True
            raise TimeoutError(
                f"{server_name} server startup timed out after {timeout_seconds:.0f}s. "
                "If this machine is doing a slow cold start, set "
                "OKTO_PULSE_STARTUP_TIMEOUT_SECONDS to a larger value."
            )
        await asyncio.sleep(0.05)


def _log_ready_servers(api_port: int, mcp_port: int) -> None:
    public_host = os.environ.get("PUBLIC_HOST", "127.0.0.1")
    public_api_port = int(os.environ.get("PUBLIC_API_PORT") or api_port)
    public_mcp_port = int(os.environ.get("PUBLIC_MCP_PORT") or mcp_port)

    _STARTUP_LOGGER.info(
        "API Server initialized successfully - http://%s:%s/api/v1",
        public_host,
        public_api_port,
    )
    _STARTUP_LOGGER.info(
        "UI Server initialized successfully - http://%s:%s",
        public_host,
        public_api_port,
    )
    _STARTUP_LOGGER.info(
        "MCP Server initialized successfully - http://%s:%s/mcp",
        public_host,
        public_mcp_port,
    )
    _STARTUP_LOGGER.info("Startup complete - The application is ready")


async def _shutdown_server_pair(
    api_server: uvicorn.Server,
    mcp_server: uvicorn.Server,
    api_task: asyncio.Task[None],
    mcp_task: asyncio.Task[None],
    *,
    timeout_seconds: float | None = None,
) -> None:
    """Stop both uvicorn servers without letting open streams hang forever."""
    timeout = _shutdown_timeout_seconds() if timeout_seconds is None else timeout_seconds
    set_shutdown_log_suppression(True)
    try:
        api_server.should_exit = True
        mcp_server.should_exit = True

        _, pending = await asyncio.wait({api_task, mcp_task}, timeout=timeout + 1.0)
        if not pending:
            return

        _STARTUP_LOGGER.warning(
            "Shutdown timeout exceeded after %.0fs; forcing API/UI and MCP servers to stop.",
            timeout,
        )

        api_server.force_exit = True
        mcp_server.force_exit = True
        api_server.should_exit = True
        mcp_server.should_exit = True
        for task in (api_task, mcp_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(api_task, mcp_task, return_exceptions=True)
    finally:
        set_shutdown_log_suppression(False)


async def _serve_dual(api_port: int, mcp_port: int) -> None:
    """Run API+UI on `api_port` and MCP on `mcp_port` inside a single
    Python process via two uvicorn `Server` instances driven by
    ``asyncio.gather``.

    Single-process is required to keep the Kùzu lock owned by exactly one
    process (the embedded DB does not support multiple writers). The two
    listeners share the same module-level state — including the
    ``_global_db`` cache, the ``_mcp_session_factory`` registered by the
    API lifespan, and the ``_active_api_key`` ``ContextVar`` — so the MCP
    sub-app sees a fully-initialised runtime.
    """
    from okto_pulse.core.mcp.server import build_mcp_asgi_app

    settings = CommunitySettings()
    uvicorn_log_config = build_uvicorn_log_config()
    shutdown_timeout = _shutdown_timeout_seconds()
    uvicorn_shutdown_timeout = int(math.ceil(shutdown_timeout))

    api_config = uvicorn.Config(
        "okto_pulse.community.main:app",
        host=settings.host,
        port=api_port,
        ws="wsproto",
        log_level="info",
        log_config=uvicorn_log_config,
        timeout_keep_alive=1,
        timeout_graceful_shutdown=uvicorn_shutdown_timeout,
    )
    api_server = uvicorn.Server(api_config)
    # Disable uvicorn's per-server signal capture — with two Servers in the
    # same loop the last one wins the process signal handler, which can leave
    # the other listener waiting forever. asyncio.Runner cancels _serve_dual on
    # Ctrl+C; we coordinate both listeners in the except block below.
    api_server.capture_signals = contextlib.nullcontext  # type: ignore[method-assign]

    mcp_config = uvicorn.Config(
        build_mcp_asgi_app(),
        # Read host from environment (set by Docker / compose) or fall back to
        # loopback so a stray process doesn't accidentally expose the MCP
        # server. Override via MCP_HOST=0.0.0.0 in docker-compose.yml when
        # port-mapping is required from outside the container.
        host=os.environ.get("MCP_HOST", "127.0.0.1"),
        port=mcp_port,
        ws="wsproto",
        log_level="info",
        log_config=uvicorn_log_config,
        timeout_keep_alive=1,
        timeout_graceful_shutdown=uvicorn_shutdown_timeout,
    )
    mcp_server = uvicorn.Server(mcp_config)
    mcp_server.capture_signals = contextlib.nullcontext  # type: ignore[method-assign]

    api_task = asyncio.create_task(api_server.serve(), name="api_ui_server")
    mcp_task = asyncio.create_task(mcp_server.serve(), name="mcp_server")

    try:
        await _wait_for_server_started("API/UI", api_server, api_task)
        await _wait_for_server_started("MCP", mcp_server, mcp_task)
        _log_ready_servers(api_port, mcp_port)
        done, _ = await asyncio.wait(
            {api_task, mcp_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            exc = task.exception()
            if exc is not None:
                raise exc
        await _shutdown_server_pair(
            api_server,
            mcp_server,
            api_task,
            mcp_task,
            timeout_seconds=shutdown_timeout,
        )
    except asyncio.CancelledError:
        # Ctrl+C reached the loop before our handler could flip should_exit.
        # Ask both servers to drain and swallow the cancel — this is the
        # expected shutdown path, not an error.
        await _shutdown_server_pair(
            api_server,
            mcp_server,
            api_task,
            mcp_task,
            timeout_seconds=shutdown_timeout,
        )
    except BaseException:
        await _shutdown_server_pair(
            api_server,
            mcp_server,
            api_task,
            mcp_task,
            timeout_seconds=shutdown_timeout,
        )
        raise


def run():
    """Run the community API + Frontend + MCP server (single process,
    two ports). Reads ``OKTO_PULSE_PORT`` / ``OKTO_PULSE_MCP_PORT`` env
    vars (set by the CLI) and falls back to the settings defaults.
    """
    settings = CommunitySettings()
    api_port = int(
        os.environ.get("PORT", os.environ.get("OKTO_PULSE_PORT", str(settings.port)))
    )
    mcp_port = int(
        os.environ.get("MCP_PORT", os.environ.get("OKTO_PULSE_MCP_PORT", str(settings.mcp_port)))
    )
    try:
        run_async_server(_serve_dual(api_port, mcp_port))
    except KeyboardInterrupt:
        # Ctrl+C / SIGINT — shutdown is graceful from here; suppress the
        # default Python traceback for a clean CLI exit.
        print("\nOkto Pulse stopped.")


if __name__ == "__main__":
    run()
