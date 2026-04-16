"""Okto Pulse Community CLI — setup and run the local-first edition."""

import warnings
warnings.filterwarnings(
    "ignore",
    message=r"urllib3.*or chardet.*doesn't match a supported version",
    category=Warning,
)

import argparse
import asyncio
import json
import os
import shutil
import signal
import socket
import sys
from multiprocessing import Process
from pathlib import Path

# Default ports
DEFAULT_API_PORT = 8100
DEFAULT_MCP_PORT = 8101


def _is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("localhost", port)) == 0


def cmd_init(args):
    """Initialize ~/.okto-pulse/ directory and seed the database."""
    from okto_pulse.community.config import CommunitySettings
    from okto_pulse.community.main import _ensure_data_dir

    mcp_port = getattr(args, "mcp_port", DEFAULT_MCP_PORT) or DEFAULT_MCP_PORT

    settings = CommunitySettings()
    if mcp_port != DEFAULT_MCP_PORT:
        settings.mcp_port = mcp_port
    _ensure_data_dir(settings)

    data_path = Path(settings.data_dir)
    print(f"Okto Pulse Community initialized at: {data_path}")
    print(f"  Database: {data_path / 'data' / 'pulse.db'}")
    print(f"  Uploads:  {data_path / 'uploads'}")

    from okto_pulse.core.infra.config import configure_settings
    from okto_pulse.core.infra.database import create_database, init_db, close_db, get_session_factory
    from okto_pulse.core.infra.auth import configure_auth
    from okto_pulse.core.infra.storage import FileSystemStorageProvider, configure_storage
    from okto_pulse.community.auth import LocalAuthProvider
    from okto_pulse.community.seed import seed_community_defaults
    from sqlalchemy import event

    configure_settings(settings)
    configure_auth(LocalAuthProvider())
    configure_storage(FileSystemStorageProvider(settings.upload_dir))
    create_database(settings.database_url, echo=False)

    from okto_pulse.core.infra.database import get_engine
    engine = get_engine()

    @event.listens_for(engine.sync_engine, "connect")
    def set_sqlite_pragmas(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async def _init():
        await init_db()
        async with get_session_factory()() as db:
            result = await seed_community_defaults(db)
            if result:
                board, agent, api_key = result
                print(f"\n  Board created: {board.name}")
                print(f"  Agent created: {agent.name}")
                print(f"  API Key: {api_key}")
                print(f"  MCP URL: http://127.0.0.1:{settings.mcp_port}/mcp?api_key={api_key}")

                mcp_config = {
                    "mcpServers": {
                        "okto-pulse": {
                            "url": f"http://127.0.0.1:{settings.mcp_port}/mcp?api_key={api_key}"
                        }
                    }
                }
                mcp_json_path = Path.cwd() / ".mcp.json"
                mcp_json_path.write_text(json.dumps(mcp_config, indent=2))
                print(f"\n  .mcp.json generated at: {mcp_json_path}")
            else:
                print("\n  Already initialized (seed exists).")
                from sqlalchemy import select
                from okto_pulse.core.models.db import Agent
                async with get_session_factory()() as db2:
                    result = await db2.execute(select(Agent).limit(1))
                    agent = result.scalar_one_or_none()
                    if agent and agent.api_key:
                        mcp_config = {
                            "mcpServers": {
                                "okto-pulse": {
                                    "url": f"http://127.0.0.1:{settings.mcp_port}/mcp?api_key={agent.api_key}"
                                }
                            }
                        }
                        mcp_json_path = Path.cwd() / ".mcp.json"
                        mcp_json_path.write_text(json.dumps(mcp_config, indent=2))
                        print(f"  .mcp.json updated at: {mcp_json_path}")

        await close_db()

    asyncio.run(_init())
    print("\nRun 'okto-pulse serve' to start the server.")


# Module-level process targets (must be picklable for Windows multiprocessing spawn).
# Port overrides are passed via environment variables since these functions take no args.
def _serve_api():
    """Start the API server. Reads OKTO_PULSE_PORT env for port override."""
    port = int(os.environ.get("OKTO_PULSE_PORT", DEFAULT_API_PORT))
    os.environ["PORT"] = str(port)
    from okto_pulse.community.main import run
    run()


def _serve_mcp():
    """Start the MCP server. Reads OKTO_PULSE_MCP_PORT env for port override."""
    port = int(os.environ.get("OKTO_PULSE_MCP_PORT", DEFAULT_MCP_PORT))
    os.environ["MCP_PORT"] = str(port)
    from okto_pulse.community.main import run_mcp
    run_mcp()


def cmd_serve(args):
    """Start backend API (+ embedded frontend) and MCP server."""
    from okto_pulse.community.main import FRONTEND_DIR

    api_port = args.api_port
    mcp_port = args.mcp_port

    if _is_port_in_use(api_port):
        print(f"Warning: Port {api_port} is already in use. API server may fail to start.")
    if _is_port_in_use(mcp_port):
        print(f"Warning: Port {mcp_port} is already in use. MCP server may fail to start.")

    has_frontend = FRONTEND_DIR.exists() and (FRONTEND_DIR / "index.html").exists()

    # Pass ports to child processes via environment variables
    os.environ["OKTO_PULSE_PORT"] = str(api_port)
    os.environ["OKTO_PULSE_MCP_PORT"] = str(mcp_port)

    api_process = Process(target=_serve_api, name="okto-pulse-api")
    mcp_process = Process(target=_serve_mcp, name="okto-pulse-mcp")

    def _shutdown(sig, frame):
        print("\nShutting down...")
        api_process.terminate()
        mcp_process.terminate()
        api_process.join(timeout=5)
        mcp_process.join(timeout=5)
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    print("Starting Okto Pulse Community...")
    if has_frontend:
        print(f"  App:  http://127.0.0.1:{api_port}  (API + Frontend)")
    else:
        print(f"  API:  http://127.0.0.1:{api_port}  (no frontend embedded)")
    print(f"  MCP:  http://127.0.0.1:{mcp_port}")
    print("  Press Ctrl+C to stop.\n")

    api_process.start()
    mcp_process.start()

    try:
        api_process.join()
        mcp_process.join()
    except KeyboardInterrupt:
        _shutdown(None, None)


def cmd_status(args):
    """Show status of Okto Pulse Community."""
    from okto_pulse.community.config import CommunitySettings

    api_port = args.api_port
    mcp_port = args.mcp_port

    settings = CommunitySettings()
    data_path = Path(settings.data_dir)
    db_path = data_path / "data" / "pulse.db"

    print("Okto Pulse Community Status")
    print(f"  Data dir: {data_path}")
    print(f"  Database: {db_path}")

    if db_path.exists():
        size_kb = db_path.stat().st_size / 1024
        print(f"  DB size:  {size_kb:.1f} KB")

        import sqlite3
        conn = sqlite3.connect(str(db_path))
        try:
            boards = conn.execute("SELECT COUNT(*) FROM boards").fetchone()[0]
            cards = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
            agents = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
            specs = conn.execute("SELECT COUNT(*) FROM specs").fetchone()[0]
            print(f"  Boards:   {boards}")
            print(f"  Cards:    {cards}")
            print(f"  Specs:    {specs}")
            print(f"  Agents:   {agents}")
        except Exception:
            print("  (tables not yet created — run 'okto-pulse init' first)")
        finally:
            conn.close()
    else:
        print("  Database not found — run 'okto-pulse init' first.")

    api_up = _is_port_in_use(api_port)
    mcp_up = _is_port_in_use(mcp_port)
    print(f"\n  API server ({api_port}):  {'running' if api_up else 'stopped'}")
    print(f"  MCP server ({mcp_port}):  {'running' if mcp_up else 'stopped'}")


def cmd_reset(args):
    """Reset all data — delete DB and uploads, re-seed."""
    from okto_pulse.community.config import CommunitySettings

    settings = CommunitySettings()
    data_path = Path(settings.data_dir)
    uploads_path = data_path / "uploads"

    if not args.yes:
        confirm = input(f"This will DELETE all data in {data_path}. Are you sure? [y/N] ")
        if confirm.lower() != "y":
            print("Aborted.")
            return

    for f in (data_path / "data").glob("pulse.db*"):
        f.unlink()
        print(f"  Deleted: {f}")

    if uploads_path.exists():
        shutil.rmtree(uploads_path)
        uploads_path.mkdir(parents=True, exist_ok=True)
        print(f"  Cleared: {uploads_path}")

    print("  Data reset complete.\n")
    cmd_init(args)


def main():
    parser = argparse.ArgumentParser(
        prog="okto-pulse",
        description="Okto Pulse Community — local-first kanban board with MCP support for AI agents",
    )

    # Global port options (shared across subcommands)
    port_group = parser.add_argument_group("port configuration")
    port_group.add_argument(
        "--api-port", type=int, default=DEFAULT_API_PORT,
        help=f"API + Frontend server port (default: {DEFAULT_API_PORT})",
    )
    port_group.add_argument(
        "--mcp-port", type=int, default=DEFAULT_MCP_PORT,
        help=f"MCP server port (default: {DEFAULT_MCP_PORT})",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # init
    sub_init = subparsers.add_parser("init", help="Initialize data directory and seed database")
    sub_init.set_defaults(func=cmd_init)

    # serve
    sub_serve = subparsers.add_parser("serve", help="Start API + Frontend + MCP servers")
    sub_serve.set_defaults(func=cmd_serve)

    # status
    sub_status = subparsers.add_parser("status", help="Show service status and DB metrics")
    sub_status.set_defaults(func=cmd_status)

    # reset
    sub_reset = subparsers.add_parser("reset", help="Delete all data and re-seed")
    sub_reset.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    sub_reset.set_defaults(func=cmd_reset)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
