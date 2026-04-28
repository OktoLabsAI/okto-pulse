"""Community edition application entry point."""

import warnings
warnings.filterwarnings(
    "ignore",
    message=r"urllib3.*or chardet.*doesn't match a supported version",
    category=Warning,
)

import asyncio
import logging
import os
import sys
import time
from datetime import timezone
from pathlib import Path
from typing import AsyncGenerator

import uvicorn
from fastapi.staticfiles import StaticFiles
from sqlalchemy import event, text

from okto_pulse.core.app import create_app
from okto_pulse.core.infra.config import configure_settings, get_settings
from okto_pulse.core.infra.database import create_database, get_engine, get_session_factory, init_db, close_db
from okto_pulse.core.infra.storage import FileSystemStorageProvider
from okto_pulse.core.kg.embedding import SentenceTransformerProvider, StubEmbeddingProvider
from okto_pulse.core.kg.interfaces.registry import configure_kg_registry, get_kg_registry
# NOTE: MCP server import moved into run_mcp() to avoid module-level settings cache
# When okto_pulse.core.mcp.server is imported, it calls get_settings() which caches
# a default instance. We need to call configure_settings() BEFORE that import happens.
from okto_pulse.community.auth import LocalAuthProvider
from okto_pulse.community.config import CommunitySettings
from okto_pulse.community.seed import seed_community_defaults

_EMBEDDING_LOGGER = logging.getLogger("okto_pulse.community.embedding")

# Preload retry policy: 3 attempts, exponential backoff (2s, 4s, 8s), 30s total budget.
# Only transient network errors retry. ImportError / OSError (disk full) / ValueError
# are deterministic and should fail fast — no amount of retrying will fix them.
_EMBEDDING_PRELOAD_ATTEMPTS = 3
_EMBEDDING_PRELOAD_BACKOFF_S = (2.0, 4.0, 8.0)
_EMBEDDING_PRELOAD_BUDGET_S = 30.0


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

    # Combined lifespan: seed data, preload embeddings, start KG workers
    async def combined_lifespan(app_instance) -> AsyncGenerator[None, None]:
        await init_db()
        async with get_session_factory()() as db:
            result = await seed_community_defaults(db)
            if result:
                board, agent, api_key = result
                print(f"\n{'='*60}")
                print(f"  Okto Pulse Community — First Boot Setup")
                print(f"{'='*60}")
                print(f"  Board created: {board.name} ({board.id})")
                print(f"  Agent created: {agent.name}")
                print(f"  API Key: {api_key}")
                print(f"  MCP URL: http://localhost:{settings.mcp_port}/mcp?api_key={api_key}")
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


def run():
    """Run the community API + Frontend server."""
    settings = CommunitySettings()

    # Read port from environment (set by CLI) or use settings
    port = int(os.environ.get("PORT", os.environ.get("OKTO_PULSE_PORT", str(settings.port))))

    uvicorn.run(
        "okto_pulse.community.main:app",
        host=settings.host,
        port=port,
        reload=settings.debug,
        ws="wsproto",
    )


def run_mcp():
    """Run the MCP server for the community edition.

    CRITICAL: Import MCP server module AFTER configure_settings() to ensure
    the correct port configuration is cached before module-level get_settings()
    calls happen.
    """
    import os
    from okto_pulse.community.config import CommunitySettings

    # Read ports from environment (set by CLI) BEFORE creating settings
    # to avoid using hardcoded defaults
    port = int(os.environ.get("MCP_PORT", os.environ.get("OKTO_PULSE_MCP_PORT", "8101")))

    # Create settings and override MCP port if env var is set
    settings = CommunitySettings()
    if port != 8101:  # Only override if env var was actually set
        settings.mcp_port = port

    _ensure_data_dir(settings)
    configure_settings(settings)
    create_database(settings.database_url, echo=settings.debug)
    _configure_sqlite_pragmas(get_engine())
    configure_kg_registry(session_factory=get_session_factory())

    # Preload the embedding model synchronously before the MCP server loop
    # starts, so the first KG call doesn't trigger a multi-second model load
    # on the event loop.
    try:
        registry = get_kg_registry()
        provider = registry.embedding_provider
        if hasattr(provider, '_get_model'):
            provider._get_model()
    except Exception:
        pass  # degrade gracefully — stub provider will be used

    # Import MCP server functions AFTER configure_settings()
    from okto_pulse.core.mcp.server import run_mcp_server, register_session_factory
    register_session_factory(get_session_factory())
    run_mcp_server()


if __name__ == "__main__":
    run()
