"""Okto Pulse Community CLI — setup and run the local-first edition."""

# ruff: noqa: E402

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
import socket
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

# Default ports
DEFAULT_API_PORT = 8100
DEFAULT_MCP_PORT = 8101

_BANNER_PATH = Path(__file__).parent / "banner.txt"


def _package_version(package_name: str) -> str:
    try:
        return version(package_name)
    except PackageNotFoundError:
        return "unknown"


def _format_version() -> str:
    return (
        f"okto-pulse {_package_version('okto-pulse')} "
        f"(okto-pulse-core {_package_version('okto-pulse-core')})"
    )


def _print_banner() -> None:
    """Print the Okto Pulse ASCII banner to stderr (kept off stdout to
    avoid corrupting JSON pipes). Suppressed when ``OKTO_PULSE_NO_BANNER``
    is set or the banner file is missing."""
    if os.environ.get("OKTO_PULSE_NO_BANNER"):
        return
    try:
        sys.stderr.write(_BANNER_PATH.read_text(encoding="utf-8"))
        sys.stderr.write("\n")
        sys.stderr.write(
            f"Version {_package_version('okto-pulse')} "
            f"({_package_version('okto-pulse-core')})\n\n"
        )
        sys.stderr.flush()
    except OSError:
        pass


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
    from sqlalchemy import event, select
    from okto_pulse.core.models.db import Board

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
        board_id = None
        async with get_session_factory()() as db:
            result = await seed_community_defaults(db)
            if result:
                board, agent, api_key = result
                board_id = board.id
                print(f"\n  Board created: {board.name}")
                print(f"  Agent created: {agent.name}")
                print(f"  API Key: {api_key}")
            else:
                print("\n  Already initialized (seed exists).")
                # Fetch the default board for KG bootstrap
                board_result = await db.execute(select(Board).limit(1))
                board_row = board_result.scalar_one_or_none()
                if board_row:
                    board_id = board_row.id

        # Bootstrap Knowledge Graph (Kuzu) for the board so the graph
        # schema and vector indexes are ready before the first agent call.
        if board_id:
            try:
                from okto_pulse.core.kg.schema import bootstrap_board_graph
                handle = bootstrap_board_graph(board_id)
                print(f"  Knowledge Graph: {handle.path} (schema {handle.schema_version})")
            except Exception as exc:
                print(f"  Knowledge Graph: bootstrap skipped ({exc})")

        await close_db()

    asyncio.run(_init())
    print("\nRun 'okto-pulse serve' to start the server.")

    # Handle --agents flag: generate .mcp.json with specified agents
    agents_param = getattr(args, "agents", None)
    if agents_param is not None:  # None = not specified, [] = specified but empty (all agents)
        _generate_mcp_json(settings.mcp_port, agents_param)


def _generate_mcp_json(mcp_port: int, agent_names: list[str] | None):
    """Generate .mcp.json with specified agents (or all if agent_names is empty)."""
    import asyncio
    from sqlalchemy import select
    from okto_pulse.core.infra.database import create_database, init_db, get_session_factory, close_db
    from okto_pulse.core.models.db import Agent
    from okto_pulse.community.config import CommunitySettings
    from okto_pulse.core.infra.auth import configure_auth
    from okto_pulse.core.infra.config import configure_settings
    from okto_pulse.community.auth import LocalAuthProvider
    from okto_pulse.core.infra.storage import FileSystemStorageProvider, configure_storage

    settings = CommunitySettings()
    configure_settings(settings)
    configure_auth(LocalAuthProvider())
    configure_storage(FileSystemStorageProvider(settings.upload_dir))
    create_database(settings.database_url, echo=False)

    async def _fetch_agents():
        await init_db()
        async with get_session_factory()() as db:
            # Fetch all active agents with API keys
            result = await db.execute(
                select(Agent).where(Agent.api_key.isnot(None)).order_by(Agent.name)
            )
            all_agents = result.scalars().all()

            if not all_agents:
                print("\n  ⚠ No agents found with API keys.")
                print("  Create agents via the web interface (Menu → Agents) first.")
                await close_db()
                return None

            # Filter by name if specified
            if agent_names:  # Specific names provided
                name_set = {name.strip() for name in agent_names}
                found_agents = [a for a in all_agents if a.name in name_set]
                missing = name_set - {a.name for a in found_agents}

                if not found_agents:
                    print(f"\n  ⚠ No matching agents found: {', '.join(sorted(name_set))}")
                    print(f"  Available agents: {', '.join(a.name for a in all_agents)}")
                    await close_db()
                    return None

                if missing:
                    print(f"\n  ⚠ Agents not found: {', '.join(sorted(missing))}")

                agents_to_export = found_agents
            else:  # No names provided = export all
                agents_to_export = all_agents

            await close_db()
            return agents_to_export

    agents = asyncio.run(_fetch_agents())
    if agents is None:
        return

    # Build mcp.json with multiple agents
    mcp_config = {"mcpServers": {}}
    for agent in agents:
        # Use a sanitized name for the server key (replace spaces with hyphens)
        server_key = agent.name.lower().replace(" ", "-").replace("_", "-")
        mcp_config["mcpServers"][server_key] = {
            "url": f"http://127.0.0.1:{mcp_port}/mcp?api_key={agent.api_key}"
        }

    mcp_json_path = Path.cwd() / ".mcp.json"
    mcp_json_path.write_text(json.dumps(mcp_config, indent=2))

    agent_list = ", ".join(f"\"{a.name}\"" for a in agents)
    print(f"\n  ✓ .mcp.json generated at: {mcp_json_path}")
    print(f"  Agents exported: {agent_list}")


def cmd_serve(args):
    """Start the API + Frontend server and the MCP server.

    Both servers run inside a single Python process (so the embedded Kùzu
    DB is owned by exactly one OS process), but listen on two different
    ports — ``--api-port`` for the REST API + UI, ``--mcp-port`` for the
    MCP transport. Each port has its own uvicorn ``Server`` instance
    driven concurrently via ``asyncio.gather``.
    """
    api_port = args.api_port
    mcp_port = args.mcp_port

    if _is_port_in_use(api_port):
        print(f"Warning: Port {api_port} is already in use. API server may fail to start.")
    if _is_port_in_use(mcp_port):
        print(f"Warning: Port {mcp_port} is already in use. MCP server may fail to start.")

    # Ports go via env so create_community_app + the MCP runner read them.
    # MUST be set BEFORE importing okto_pulse.community.main — that module
    # evaluates `app = create_community_app()` at import time, which reads
    # the env vars to inject /config.js with the correct API_URL/MCP_URL.
    os.environ["OKTO_PULSE_PORT"] = str(api_port)
    os.environ["OKTO_PULSE_MCP_PORT"] = str(mcp_port)

    from okto_pulse.community.main import FRONTEND_DIR
    has_frontend = FRONTEND_DIR.exists() and (FRONTEND_DIR / "index.html").exists()

    # Terms-of-Use pre-acceptance via CLI flag or env var.
    if getattr(args, "accept_terms", False):
        os.environ["OKTO_PULSE_TERMS_ACCEPTED"] = "1"
        from okto_pulse.community.acceptance import write_acceptance
        rec = write_acceptance("cli")
        print(f"Terms-of-Use pre-accepted via --accept-terms (version {rec['version']}).")
    elif (os.environ.get("OKTO_PULSE_TERMS_ACCEPTED") or "").strip() == "1":
        from okto_pulse.community.acceptance import write_acceptance, read_acceptance
        if read_acceptance() is None:
            rec = write_acceptance("env")
            print(f"Terms-of-Use pre-accepted via env (version {rec['version']}).")

    print("Starting Okto Pulse Community...")
    if has_frontend:
        print(f"  App:  http://127.0.0.1:{api_port}  (API + Frontend)")
    else:
        print(f"  API:  http://127.0.0.1:{api_port}  (no frontend embedded)")
    print(f"  MCP:  http://127.0.0.1:{mcp_port}/mcp")
    print("  Press Ctrl+C to stop.\n")

    # Single-process, dual-port: run() spawns two uvicorn Server instances
    # via asyncio.gather. uvicorn handles SIGINT/SIGTERM natively for both.
    from okto_pulse.community.main import run
    run()


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


def cmd_api_key(args):
    """Print the bootstrap API key (the dash_<hex> seeded by `okto-pulse init`).

    Reads directly from the SQLite database to avoid coupling to the
    running API server. Used by the release pipeline (release.yml) to
    extract the key for replay smoke tests without grepping log output.

    Exit codes:
      0 — key printed
      1 — DB missing, no agents seeded, or agent has no api_key

    Output format: a single line containing the key on stdout. Banner
    goes to stderr so this is safe to pipe.
    """
    import sqlite3
    from okto_pulse.community.config import CommunitySettings

    settings = CommunitySettings()
    db_path = Path(settings.data_dir) / "data" / "pulse.db"

    if not db_path.exists():
        print(f"Database not found at {db_path}. Run 'okto-pulse init' first.", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    try:
        # The default seed creates exactly one agent ("Local Agent") with a
        # bootstrap dash_<hex> key. Take the oldest seeded agent so we keep
        # returning the same value across restarts even if more agents are
        # added later.
        row = conn.execute(
            "SELECT api_key FROM agents WHERE api_key IS NOT NULL "
            "ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError as exc:
        print(f"Database not initialised: {exc}. Run 'okto-pulse init' first.", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()

    if row is None or not row[0]:
        print("No bootstrap API key found in database.", file=sys.stderr)
        sys.exit(1)

    print(row[0])


def cmd_verify_pipeline(args):
    """Run the 5 pipeline health checks against a board.

    Opens a short-lived DB session, calls the pure check functions in
    ``okto_pulse.core.kg.health`` and renders either a compact table (default)
    or JSON (``--json``). Exit code 0 iff every layer reports ``healthy=True``.
    """
    from okto_pulse.community.config import CommunitySettings
    from okto_pulse.core.infra.config import configure_settings
    from okto_pulse.core.infra.database import (
        create_database,
        get_session_factory,
        init_db,
        close_db,
    )
    from okto_pulse.core.kg.health import (
        check_global,
        check_kuzu,
        check_kuzu_node_refs,
        check_outbox,
        check_queue,
    )
    from okto_pulse.core.kg.interfaces.registry import configure_kg_registry

    board_id: str = args.board_id
    emit_json: bool = bool(getattr(args, "json", False))

    settings = CommunitySettings()
    configure_settings(settings)
    create_database(settings.database_url, echo=False)

    async def _run() -> list:
        await init_db()
        factory = get_session_factory()
        configure_kg_registry(session_factory=factory)
        try:
            async with factory() as db:
                queue_h = await check_queue(db, board_id)
                kuzu_h = check_kuzu(board_id)
                refs_h = await check_kuzu_node_refs(
                    db, board_id, kuzu_total=kuzu_h.counts.get("total")
                )
                outbox_h = await check_outbox(db, board_id)
                global_h = check_global(board_id)
            return [queue_h, kuzu_h, refs_h, outbox_h, global_h]
        finally:
            await close_db()

    layers = asyncio.run(_run())

    if emit_json:
        payload = {
            "board_id": board_id,
            "all_healthy": all(L.healthy for L in layers),
            "layers": [
                {
                    "layer": L.layer,
                    "healthy": L.healthy,
                    "counts": L.counts,
                    "details": L.details,
                }
                for L in layers
            ],
        }
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(f"Pipeline health for board {board_id}")
        name_w = max(len(L.layer) for L in layers)
        for L in layers:
            mark = "OK " if L.healthy else "BAD"
            print(f"  [{mark}] {L.layer.ljust(name_w)}  {L.details}")
        ok_count = sum(1 for L in layers if L.healthy)
        print(f"\n  {ok_count}/{len(layers)} layers healthy")

    sys.exit(0 if all(L.healthy for L in layers) else 1)


def cmd_kg_backfill(args):
    """Run the Layer 1 deterministic worker against a board.

    In `--dry-run` mode (default) prints a diff of what WOULD be emitted
    without touching Kùzu — satisfies the `CLI dry-run reporta diff sem
    escrever` business rule of spec c48a5c33. `--apply` flips to write
    mode (requires feature flag `kg_consolidation_v2` enabled on the board).
    """
    from okto_pulse.community.config import CommunitySettings
    from okto_pulse.core.infra.config import configure_settings
    from okto_pulse.core.infra.database import (
        create_database,
        get_session_factory,
        init_db,
        close_db,
    )
    from okto_pulse.core.kg.workers.deterministic_worker import DeterministicWorker
    from okto_pulse.core.models.db import Card, Spec, Sprint
    from sqlalchemy import select

    board_id: str = args.board_id
    apply_writes: bool = bool(getattr(args, "apply", False))
    artifact_filter: str = getattr(args, "artifact_type", "") or ""
    emit_json: bool = bool(getattr(args, "json", False))

    settings = CommunitySettings()
    configure_settings(settings)
    create_database(settings.database_url, echo=False)

    # ── Path B: Apply ────────────────────────────────────────────────
    if apply_writes:
        asyncio.run(_apply_backfill(board_id, emit_json, settings))
        sys.exit(0)

    # ── Path A: Dry-run (unchanged) ──────────────────────────────────
    async def _load() -> dict:
        await init_db()
        factory = get_session_factory()
        try:
            async with factory() as db:
                specs_q = select(Spec).where(Spec.board_id == board_id)
                sprints_q = select(Sprint).where(Sprint.board_id == board_id)
                cards_q = select(Card).where(Card.board_id == board_id)
                spec_rows = (await db.execute(specs_q)).scalars().all()
                sprint_rows = (await db.execute(sprints_q)).scalars().all()
                card_rows = (await db.execute(cards_q)).scalars().all()
                return {
                    "specs": [_spec_to_dict(s) for s in spec_rows],
                    "sprints": [_sprint_to_dict(s) for s in sprint_rows],
                    "cards": [_card_to_dict(c) for c in card_rows],
                }
        finally:
            await close_db()

    data = asyncio.run(_load())

    worker = DeterministicWorker()
    summary = {
        "board_id": board_id,
        "dry_run": True,
        "artifacts": {"spec": 0, "sprint": 0, "card": 0},
        "nodes_total": 0,
        "edges_total": 0,
        "missing_link_candidates": 0,
        "per_artifact": [],
    }

    targets: list[tuple[str, dict]] = []
    if artifact_filter in ("", "spec"):
        targets.extend(("spec", s) for s in data["specs"])
    if artifact_filter in ("", "sprint"):
        targets.extend(("sprint", s) for s in data["sprints"])
    if artifact_filter in ("", "card"):
        targets.extend(("card", c) for c in data["cards"])

    for art_type, artifact in targets:
        try:
            result = worker.process_artifact(art_type, artifact)
        except Exception as exc:
            summary["per_artifact"].append({
                "artifact_type": art_type, "artifact_id": artifact.get("id"),
                "error": str(exc),
            })
            continue
        summary["artifacts"][art_type] += 1
        summary["nodes_total"] += len(result.nodes)
        summary["edges_total"] += len(result.edges)
        summary["missing_link_candidates"] += len(result.missing_link_candidates)
        summary["per_artifact"].append({
            "artifact_type": art_type,
            "artifact_id": artifact.get("id"),
            "nodes": len(result.nodes),
            "edges": len(result.edges),
            "missing_link_candidates": len(result.missing_link_candidates),
            "deterministic_edge_ratio": result.deterministic_edge_ratio(),
            "content_hash": result.content_hash,
        })

    if emit_json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        print(f"KG backfill [DRY-RUN] for board {board_id}")
        print("  Artifacts scanned:")
        for k, v in summary["artifacts"].items():
            print(f"    {k:<8} {v}")
        print(f"  Nodes to emit: {summary['nodes_total']}")
        print(f"  Edges to emit: {summary['edges_total']}")
        print(f"  Missing link candidates: {summary['missing_link_candidates']}")

    sys.exit(0)


async def _apply_backfill(board_id: str, emit_json: bool, settings) -> None:
    """Apply path: enqueue all artifacts and drain the consolidation queue."""
    from okto_pulse.core.infra.database import (
        get_session_factory,
        init_db,
        close_db,
    )
    from okto_pulse.core.kg.interfaces.registry import configure_kg_registry
    from okto_pulse.core.kg.schema import bootstrap_board_graph
    from okto_pulse.core.kg.governance import start_historical_consolidation
    from okto_pulse.core.kg.workers.consolidation import ConsolidationWorker
    from okto_pulse.core.models.db import ConsolidationQueue as CQ
    from sqlalchemy import select

    await init_db()
    factory = get_session_factory()
    configure_kg_registry(session_factory=factory)

    try:
        # Bootstrap Kùzu graph schema for this board
        bootstrap_board_graph(board_id)

        # Enqueue all artifacts via governance
        async with factory() as db:
            result = await start_historical_consolidation(db, board_id)
        total_queued = result.get("total_artifacts", 0)

        if total_queued == 0 and result.get("status") != "already_in_progress":
            if emit_json:
                print(json.dumps({
                    "board_id": board_id,
                    "status": "no_artifacts",
                    "total_queued": 0,
                    "total_processed": 0,
                    "failed_count": 0,
                }))
            else:
                print(f"KG backfill [APPLY] for board {board_id}")
                print("  No eligible artifacts found (need done/approved specs or closed sprints)")
            return

        # Drain the queue via ConsolidationWorker
        worker = ConsolidationWorker(factory)
        total_processed = 0
        batch_num = 0
        while True:
            processed = await worker.process_batch()
            if processed == 0:
                break
            batch_num += 1
            total_processed += processed
            if not emit_json:
                print(f"  Batch {batch_num}: processed {processed} entries")

        # Check for failures
        async with factory() as db:
            failed = (await db.execute(
                select(CQ).where(CQ.board_id == board_id, CQ.status == "failed")
            )).scalars().all()
        failed_count = len(failed)

        if emit_json:
            output = {
                "board_id": board_id,
                "status": "already_in_progress" if result.get("status") == "already_in_progress" else "completed",
                "total_queued": total_queued,
                "total_processed": total_processed,
                "failed_count": failed_count,
            }
            if failed:
                output["failures"] = [
                    {
                        "artifact_type": f.artifact_type,
                        "artifact_id": f.artifact_id,
                        "error": getattr(f, "error_message", None) or getattr(f, "last_error", None) or "unknown",
                    }
                    for f in failed
                ]
            print(json.dumps(output, indent=2, default=str))
        else:
            print(f"KG backfill [APPLY] for board {board_id}")
            print(f"  Artifacts queued:   {total_queued}")
            print(f"  Artifacts processed: {total_processed}")
            if failed_count:
                print(f"  Failed:              {failed_count}")
                for f in failed:
                    err = getattr(f, "error_message", None) or getattr(f, "last_error", None) or "unknown"
                    print(f"    - {f.artifact_type}/{f.artifact_id}: {err}")
            else:
                print("  All entries processed successfully")

    finally:
        await close_db()


def _spec_to_dict(s):
    return {
        "id": s.id,
        "title": s.title,
        "description": s.description,
        "context": s.context,
        "functional_requirements": s.functional_requirements,
        "technical_requirements": s.technical_requirements,
        "acceptance_criteria": s.acceptance_criteria,
        "test_scenarios": s.test_scenarios,
        "business_rules": s.business_rules,
        "api_contracts": s.api_contracts,
    }


def _sprint_to_dict(s):
    return {
        "id": s.id,
        "title": s.title,
        "description": s.description,
        "objective": getattr(s, "objective", None),
        "expected_outcome": getattr(s, "expected_outcome", None),
        "spec_id": s.spec_id,
    }


def _card_to_dict(c):
    p = getattr(c, "priority", None)
    return {
        "id": c.id,
        "title": c.title,
        "description": c.description,
        "card_type": str(c.card_type) if c.card_type else "normal",
        "origin_task_id": getattr(c, "origin_task_id", None),
        "sprint_id": getattr(c, "sprint_id", None),
        "spec_id": c.spec_id,
        "priority": str(p.value) if hasattr(p, "value") and p is not None else None,
    }


def cmd_kg_dedup_entities(args):
    """NC-8 (spec 7f23535f) — consolidate duplicate Kuzu nodes per
    (node_type, source_artifact_ref).

    Default writes to the graph; pass --dry-run for a no-op preview.
    Output is a human-readable table by default; --json switches to
    structured output for ops automation.
    """
    from okto_pulse.community.config import CommunitySettings
    from okto_pulse.core.infra.config import configure_settings
    from okto_pulse.core.kg.dedup_migration import (
        format_report_table,
        migrate_dedup_entities,
    )

    board_id: str = args.board_id
    dry_run: bool = bool(getattr(args, "dry_run", False))
    emit_json: bool = bool(getattr(args, "json", False))

    settings = CommunitySettings()
    configure_settings(settings)

    report = migrate_dedup_entities(board_id, dry_run=dry_run)

    if emit_json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(format_report_table(report))
    sys.exit(0)


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
    parser.add_argument(
        "--version",
        action="version",
        version=_format_version(),
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # init
    sub_init = subparsers.add_parser("init", help="Initialize data directory and seed database")
    sub_init.add_argument(
        "--agents",
        nargs="*",
        metavar="NAME",
        help="Export specific agents to .mcp.json (comma-separated names, or all if empty)",
    )
    sub_init.set_defaults(func=cmd_init)

    # serve
    sub_serve = subparsers.add_parser("serve", help="Start API + Frontend + MCP servers")
    sub_serve.add_argument(
        "--api-port", type=int, default=DEFAULT_API_PORT,
        help=f"API + Frontend server port (default: {DEFAULT_API_PORT})",
    )
    sub_serve.add_argument(
        "--mcp-port", type=int, default=DEFAULT_MCP_PORT,
        help=f"MCP server port (default: {DEFAULT_MCP_PORT})",
    )
    sub_serve.add_argument(
        "--accept-terms",
        action="store_true",
        help="Pre-accept the Terms-of-Use & License (skips the first-run modal). "
             "Equivalent to setting OKTO_PULSE_TERMS_ACCEPTED=1.",
    )
    sub_serve.set_defaults(func=cmd_serve)

    # status
    sub_status = subparsers.add_parser("status", help="Show service status and DB metrics")
    sub_status.add_argument(
        "--api-port", type=int, default=DEFAULT_API_PORT,
        help=f"API server port (default: {DEFAULT_API_PORT})",
    )
    sub_status.add_argument(
        "--mcp-port", type=int, default=DEFAULT_MCP_PORT,
        help=f"MCP server port (default: {DEFAULT_MCP_PORT})",
    )
    sub_status.set_defaults(func=cmd_status)

    # api-key — print bootstrap dash_<hex> from the seeded DB.
    sub_apikey = subparsers.add_parser(
        "api-key",
        help="Print the bootstrap API key (dash_<hex>) seeded by 'okto-pulse init'",
    )
    sub_apikey.set_defaults(func=cmd_api_key)

    # reset
    sub_reset = subparsers.add_parser("reset", help="Delete all data and re-seed")
    sub_reset.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    sub_reset.set_defaults(func=cmd_reset)

    # verify-pipeline
    sub_verify = subparsers.add_parser(
        "verify-pipeline",
        help="Run health checks on all 5 Kanban-KG pipeline layers for a board",
    )
    sub_verify.add_argument(
        "board_id",
        help="Board ID to inspect (UUID string — see 'okto-pulse status')",
    )
    sub_verify.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of the default table",
    )
    sub_verify.set_defaults(func=cmd_verify_pipeline)

    # kg — knowledge graph operations (backfill, migrate, metrics wire-up later)
    sub_kg = subparsers.add_parser(
        "kg",
        help="Knowledge graph operations (Layer 1 backfill, migration, metrics)",
    )
    kg_subparsers = sub_kg.add_subparsers(dest="kg_command", help="KG sub-commands")

    sub_backfill = kg_subparsers.add_parser(
        "backfill",
        help="Re-extract all deterministic nodes + edges for a board (dry-run by default)",
    )
    sub_backfill.add_argument("board_id", help="Target board UUID")
    sub_backfill.add_argument(
        "--apply", action="store_true",
        help="Apply writes to Kùzu (default: dry-run diff only)",
    )
    sub_backfill.add_argument(
        "--artifact-type", default="",
        choices=("", "spec", "sprint", "card"),
        help="Limit to one artifact type (default: all)",
    )
    sub_backfill.add_argument(
        "--json", action="store_true",
        help="Emit machine-readable JSON instead of table",
    )
    sub_backfill.set_defaults(func=cmd_kg_backfill)

    # NC-8 (spec 7f23535f) — dedup-entities migration
    sub_dedup = kg_subparsers.add_parser(
        "dedup-entities",
        help="Consolidate duplicate Kuzu nodes per (node_type, source_artifact_ref)",
    )
    sub_dedup.add_argument("board_id", help="Target board UUID")
    sub_dedup.add_argument(
        "--dry-run", action="store_true",
        help="Report duplicates without modifying the graph",
    )
    sub_dedup.add_argument(
        "--json", action="store_true",
        help="Emit machine-readable JSON instead of table",
    )
    sub_dedup.set_defaults(func=cmd_kg_dedup_entities)

    args = parser.parse_args()
    if not args.command:
        _print_banner()
        parser.print_help()
        sys.exit(1)
    if args.command == "kg" and not getattr(args, "kg_command", None):
        _print_banner()
        sub_kg.print_help()
        sys.exit(1)

    _print_banner()
    args.func(args)


if __name__ == "__main__":
    main()
