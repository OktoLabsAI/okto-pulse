"""Read-only CLI status with one observation shared by text and JSON output."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

from okto_pulse.community.serve_lock import inspect_serve_lock_identity


def collect_status(settings, *, api_port: int, mcp_port: int, port_probe) -> dict:
    data_path = Path(settings.data_dir).expanduser().absolute()
    db_path = data_path / "data" / "pulse.db"
    report = {
        "data_dir": str(data_path),
        "source": settings.data_dir_origin,
        "db_path": str(db_path),
        "db_size_kb": None,
        "boards": None,
        "cards": None,
        "specs": None,
        "agents": None,
        "database_status": "missing",
        "error": None,
    }
    try:
        if db_path.exists():
            report["db_size_kb"] = db_path.stat().st_size / 1024
            # mode=ro prevents accidental creation; BEGIN pins all four counts.
            with closing(
                sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=1)
            ) as conn:
                conn.execute("BEGIN")
                counts = {
                    table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("boards", "cards", "specs", "agents")
                }
                report.update(counts, database_status="ready")
    except (sqlite3.Error, OSError) as exc:
        if isinstance(exc, sqlite3.OperationalError) and str(exc).startswith(
            "no such table:"
        ):
            report["database_status"] = "uninitialized"
        else:
            report["database_status"] = "error"
            report["error"] = {"type": type(exc).__name__, "message": str(exc)}
    report.update(
        api_up=port_probe(api_port),
        mcp_up=port_probe(mcp_port),
        runtime_identity=inspect_serve_lock_identity(settings),
    )
    return report


def render_status(
    report: dict, *, json_output: bool, api_port: int, mcp_port: int
) -> int:
    if json_output:
        print(json.dumps(report, sort_keys=True, allow_nan=False))
        return int(report["error"] is not None)
    print("Okto Pulse Community Status")
    print(f"  Data dir: {report['data_dir']}")
    print(f"  Source:   {report['source']}")
    print(f"  Database: {report['db_path']}")
    if report["db_size_kb"] is not None:
        print(f"  DB size:  {report['db_size_kb']:.1f} KB")
    state = report["database_status"]
    if state == "ready":
        for name in ("Boards", "Cards", "Specs", "Agents"):
            print(f"  {name + ':':10}{report[name.lower()]}")
    elif state == "missing":
        print("  Database not found — run 'okto-pulse init' first.")
    elif state == "uninitialized":
        print("  (tables not yet created — run 'okto-pulse init' first)")
    else:
        error = report["error"]
        print(
            f"ERROR [status]: {report['db_path']}: {error['type']}: {error['message']}",
            file=sys.stderr,
        )
    identity = report["runtime_identity"]
    identity_state = identity["state"]
    if identity_state == "confirmed":
        label = f"confirmed (instance {identity['instance_id']})"
    elif identity_state == "unreadable":
        label = "unreadable (serve lock cannot be verified)"
    elif identity_state == "identity_mismatch":
        label = f"identity mismatch ({identity['reason']})"
    elif report["api_up"] or report["mcp_up"]:
        label = f"unknown ({identity['reason']})"
    else:
        label = f"stopped ({identity['reason']})"
    print(f"  Runtime identity: {label}")
    print(
        f"\n  API server ({api_port}):  {'running' if report['api_up'] else 'stopped'}"
    )
    print(f"  MCP server ({mcp_port}):  {'running' if report['mcp_up'] else 'stopped'}")
    return int(report["error"] is not None)
