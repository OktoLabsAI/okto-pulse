"""Community edition application entry point."""

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

import uvicorn
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import event, text

from okto_pulse.core.app import create_app
from okto_pulse.core.infra.config import configure_settings
from okto_pulse.core.infra.database import create_database, get_engine, get_session_factory, init_db, close_db
from okto_pulse.core.infra.storage import FileSystemStorageProvider
from okto_pulse.core.mcp.server import register_session_factory, run_mcp_server as _core_run_mcp
from okto_pulse.community.auth import LocalAuthProvider
from okto_pulse.community.config import CommunitySettings
from okto_pulse.community.seed import seed_community_defaults

# Frontend dist embedded in the package (built with VITE_AUTH_MODE=local)
FRONTEND_DIR = Path(__file__).parent / "frontend_dist"


def _ensure_data_dir(settings: CommunitySettings) -> None:
    """Create data directory structure if it doesn't exist."""
    data_path = Path(settings.data_dir)
    for subdir in [data_path, data_path / "data", data_path / "uploads"]:
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


def _mount_frontend(app, frontend_dir: Path) -> None:
    """Mount the pre-built frontend SPA on the FastAPI app.

    Uses Starlette middleware approach to avoid route-ordering issues:
    - /assets/* → StaticFiles mount (correct MIME types)
    - /api/*, /health, /docs, /openapi.json → pass through to FastAPI
    - Everything else → index.html (SPA routing)
    """
    if not frontend_dir.exists():
        return

    assets_dir = frontend_dir / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="frontend-assets")

    index_html = frontend_dir / "index.html"
    if not index_html.exists():
        return

    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import Response as StarletteResponse

    _API_PREFIXES = ("/api/", "/health", "/docs", "/openapi.json", "/redoc", "/mcp")

    class SPAMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            response = await call_next(request)
            # If the response is 404 and the path is not an API/asset path,
            # serve index.html for SPA client-side routing
            if response.status_code == 404:
                path = request.url.path
                if not any(path.startswith(p) for p in _API_PREFIXES) and not path.startswith("/assets"):
                    return FileResponse(str(index_html))
            return response

    app.add_middleware(SPAMiddleware)


def create_community_app():
    """Create the community FastAPI application with embedded frontend."""
    settings = CommunitySettings()
    _ensure_data_dir(settings)

    auth = LocalAuthProvider()
    storage = FileSystemStorageProvider(settings.upload_dir)

    app = create_app(
        settings=settings,
        auth_provider=auth,
        storage_provider=storage,
        cors_origins=settings.cors_origins_list,
    )

    # Configure SQLite pragmas AFTER create_database was called by create_app
    _configure_sqlite_pragmas(get_engine())

    # Mount frontend (must be AFTER API routes so /api/v1/* takes precedence)
    _mount_frontend(app, FRONTEND_DIR)

    # Override lifespan to add seed
    @asynccontextmanager
    async def community_lifespan(app_instance) -> AsyncGenerator[None, None]:
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
        yield
        await close_db()

    app.router.lifespan_context = community_lifespan

    return app


# Module-level app created on import — uvicorn needs "module:app" reference.
# The print was moved to cmd_serve in cli.py to avoid showing wrong port.
app = create_community_app()


def run():
    """Run the community API + Frontend server."""
    settings = CommunitySettings()
    uvicorn.run(
        "okto_pulse.community.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )


def run_mcp():
    """Run the MCP server for the community edition."""
    settings = CommunitySettings()
    _ensure_data_dir(settings)
    configure_settings(settings)
    create_database(settings.database_url, echo=settings.debug)
    _configure_sqlite_pragmas(get_engine())
    register_session_factory(get_session_factory())
    _core_run_mcp()


if __name__ == "__main__":
    run()
