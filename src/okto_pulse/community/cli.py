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
import logging
import os
import shutil
import socket
import sys
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Literal

# Default ports
DEFAULT_API_PORT = 8100
DEFAULT_MCP_PORT = 8101

_BANNER_PATH = Path(__file__).parent / "banner.txt"
_METRICS_CLI_LOGGER = logging.getLogger("okto_pulse.community.metrics.cli")

_CredentialSource = Literal["governed_legacy_plaintext", "reveal_once"]


@dataclass(frozen=True)
class _ExportableAgentCredential:
    name: str
    plaintext: str
    source: _CredentialSource


def _is_recoverable_agent_key(value: str | None) -> bool:
    return _stored_agent_credential_source(value) == "governed_legacy_plaintext"


def _stored_agent_credential_source(value: str | None) -> _CredentialSource | None:
    if value and value.startswith("dash_"):
        return "governed_legacy_plaintext"
    return None


def _exportable_credential_from_legacy_agent(
    agent,
) -> _ExportableAgentCredential | None:
    plaintext = _field(agent, "api_key")
    if _stored_agent_credential_source(plaintext) != "governed_legacy_plaintext":
        return None
    return _ExportableAgentCredential(
        name=_field(agent, "name"),
        plaintext=plaintext,
        source="governed_legacy_plaintext",
    )


def _exportable_credential_from_reveal_once(
    name: str, plaintext: str
) -> _ExportableAgentCredential | None:
    if not plaintext.startswith("dash_"):
        return None
    return _ExportableAgentCredential(
        name=name, plaintext=plaintext, source="reveal_once"
    )


def _field(record, name: str, default=None):
    if isinstance(record, dict):
        return record.get(name, default)
    mapping = getattr(record, "_mapping", None)
    if mapping is not None and name in mapping:
        return mapping[name]
    return getattr(record, name, default)


def _json_field(record, name: str, default=None):
    value = _field(record, name, default)
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return default
    return value


def _result_records(result):
    mappings = getattr(result, "mappings", None)
    if callable(mappings):
        return list(mappings().all())
    scalars = getattr(result, "scalars", None)
    if callable(scalars):
        return list(scalars().all())
    return list(result.all())


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


def _configure_community_relational_runtime(settings, *, echo: bool = False) -> None:
    from okto_pulse.community.adapters.sqlalchemy_database import (
        configure_community_database,
    )

    configure_community_database(settings.database_url, echo=echo)


def _fail_fast_if_server_running(operation: str) -> None:
    """KGD-01 C6 (S10) — serve-lock na CLI.

    Entrypoints que abrem grafos de board (``init``, ``kg backfill --apply``,
    ``kg dedup-entities``, ``verify-pipeline``) falham rápido (<5s) com erro
    claro quando um servidor vivo possui o serve-lock do data dir: dois
    processos sobre o mesmo ``graph.lbug`` são o produtor do "escritor
    stale" que corrompe o WAL (KB1/H3). Takeover implícito só acontece com
    heartbeat stale E PID comprovadamente morto (ver ``serve_lock``).
    ``kg restore --apply`` já é coberto pelo check de serve-lock do próprio
    adapter (C4, erro estruturado ``board_locked``).
    """
    from okto_pulse.community.config import CommunitySettings
    from okto_pulse.community.serve_lock import (
        ServeAlreadyRunningError,
        assert_no_live_server,
    )

    try:
        assert_no_live_server(CommunitySettings().data_dir, operation=operation)
    except ServeAlreadyRunningError as exc:
        print(
            f"ERROR [serve-lock]: refusing '{operation}' while an okto-pulse "
            f"server is running.\n{exc}",
            file=sys.stderr,
        )
        sys.exit(2)


def cmd_init(args):
    """Initialize ~/.okto-pulse/ directory and seed the database."""
    # KGD-01 C6 (S10): init bootstrapa o grafo do board — nunca com o
    # servidor vivo segurando o mesmo graph.lbug.
    _fail_fast_if_server_running("init")

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
    from okto_pulse.core.ports.relational_runtime import (
        close_db,
        get_session_factory,
        init_db,
    )
    from okto_pulse.core.infra.auth import configure_auth
    from okto_pulse.core.infra.storage import configure_storage
    from okto_pulse.community.adapters.composition import community_storage_provider
    from okto_pulse.community.auth import LocalAuthProvider
    from okto_pulse.community.seed import seed_community_defaults
    from sqlalchemy import text as sa_text

    configure_settings(settings)
    configure_auth(LocalAuthProvider())
    configure_storage(community_storage_provider(settings.upload_dir))

    # R01C REPLAN-IMP4 (FR3/FR5): register the Community schema-lifecycle
    # orchestrator BEFORE init_db so the core delegates the migrate->create_all->seed
    # lifecycle to the edition (same migrator+bootstrapper as the serve path).
    from okto_pulse.community.adapters.relational_schema_lifecycle import (
        register_community_relational_schema_lifecycle,
    )

    register_community_relational_schema_lifecycle()
    _configure_community_relational_runtime(settings, echo=False)

    async def _init():
        revealed_agents: list[tuple[str, str]] = []
        await init_db()
        board_id = None
        async with get_session_factory()() as db:
            result = await seed_community_defaults(db)
            if result:
                board, agent, api_key = result
                revealed_agents.append((agent.name, api_key))
                board_id = board.id
                print(f"\n  Board created: {board.name}")
                print(f"  Agent created: {agent.name}")
                print(f"  API Key: {api_key}")
            else:
                print("\n  Already initialized (seed exists).")
                # Fetch the default board for KG bootstrap
                board_result = await db.execute(
                    sa_text("SELECT id FROM boards ORDER BY created_at, id LIMIT 1")
                )
                board_row = board_result.mappings().first()
                if board_row:
                    board_id = board_row["id"]

        # Bootstrap Knowledge Graph (Kuzu) for the board so the graph
        # schema and vector indexes are ready before the first agent call.
        if board_id:
            try:
                # Schema lifecycle crosses the Core port; the CLI resolves the
                # Community-local path only for operator-facing diagnostics.
                from okto_pulse.community.adapters.kg_runtime import board_kuzu_path
                from okto_pulse.core.services.application_kg import (
                    get_current_provider_registry,
                )

                _kg_reg = get_current_provider_registry()
                await _kg_reg.graph_schema_manager.ensure_bootstrapped(board_id)
                _kg_path = board_kuzu_path(board_id)
                _kg_ver = await _kg_reg.graph_schema_manager.current_version(board_id)
                print(f"  Knowledge Graph: {_kg_path} (schema {_kg_ver})")
            except Exception as exc:
                print(f"  Knowledge Graph: bootstrap skipped ({exc})")

        await close_db()
        return revealed_agents

    revealed_agents = asyncio.run(_init())
    print("\nRun 'okto-pulse serve' to start the server.")

    # Handle --agents flag: generate .mcp.json with specified agents
    agents_param = getattr(args, "agents", None)
    if (
        agents_param is not None
    ):  # None = not specified, [] = specified but empty (all agents)
        _generate_mcp_json(
            settings.mcp_port, agents_param, revealed_agents=revealed_agents
        )


def _generate_mcp_json(
    mcp_port: int,
    agent_names: list[str] | None,
    revealed_agents: list[tuple[str, str]] | None = None,
):
    """Generate .mcp.json with specified agents (or all if agent_names is empty)."""
    import asyncio
    from sqlalchemy import text as sa_text
    from okto_pulse.core.ports.relational_runtime import (
        close_db,
        get_session_factory,
        init_db,
    )
    from okto_pulse.community.config import CommunitySettings
    from okto_pulse.core.infra.auth import configure_auth
    from okto_pulse.core.infra.config import configure_settings
    from okto_pulse.community.auth import LocalAuthProvider
    from okto_pulse.core.infra.storage import configure_storage
    from okto_pulse.community.adapters.composition import community_storage_provider

    settings = CommunitySettings()
    configure_settings(settings)
    configure_auth(LocalAuthProvider())
    configure_storage(community_storage_provider(settings.upload_dir))
    _configure_community_relational_runtime(settings, echo=False)
    # R01C REPLAN-IMP4: Community owns the schema lifecycle here too — register
    # the orchestrator so this command's init_db delegates to the edition
    # migrator+bootstrapper (idempotent; same lifecycle as serve/init).
    from okto_pulse.community.adapters.relational_schema_lifecycle import (
        register_community_relational_schema_lifecycle,
    )

    register_community_relational_schema_lifecycle()

    async def _fetch_agents():
        await init_db()
        async with get_session_factory()() as db:
            # Fetch all active agents with API keys
            result = await db.execute(
                sa_text(
                    "SELECT name, api_key FROM agents "
                    "WHERE api_key IS NOT NULL ORDER BY name"
                )
            )
            all_agents = _result_records(result)
            exportable_by_name: dict[str, _ExportableAgentCredential] = {}
            for agent in all_agents:
                credential = _exportable_credential_from_legacy_agent(agent)
                if credential is not None:
                    exportable_by_name[credential.name] = credential
            for name, key in revealed_agents or []:
                credential = _exportable_credential_from_reveal_once(name, key)
                if credential is not None:
                    exportable_by_name[credential.name] = credential
            exportable_agents = list(exportable_by_name.values())
            all_agent_names = {_field(a, "name") for a in all_agents} | {
                name for name, _key in revealed_agents or []
            }

            if not exportable_agents:
                print("\n  ⚠ No recoverable agent API keys found.")
                print(
                    "  Newly created keys are reveal-once; regenerate one in the UI/API if needed."
                )
                await close_db()
                return None

            # Filter by name if specified
            if agent_names:  # Specific names provided
                name_set = {name.strip() for name in agent_names}
                found_agents = [a for a in exportable_agents if a.name in name_set]
                missing = name_set - all_agent_names
                unrecoverable = (all_agent_names & name_set) - set(exportable_by_name)

                if not found_agents:
                    print(
                        f"\n  ⚠ No matching agents found: {', '.join(sorted(name_set))}"
                    )
                    print(
                        f"  Available exportable agents: {', '.join(a.name for a in exportable_agents)}"
                    )
                    await close_db()
                    return None

                if missing:
                    print(f"\n  ⚠ Agents not found: {', '.join(sorted(missing))}")
                if unrecoverable:
                    print(
                        "\n  ⚠ Agents skipped because their keys are reveal-once only: "
                        f"{', '.join(sorted(unrecoverable))}"
                    )

                agents_to_export = found_agents
            else:  # No names provided = export all
                agents_to_export = exportable_agents

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
            "url": f"http://127.0.0.1:{mcp_port}/mcp?api_key={agent.plaintext}"
        }

    mcp_json_path = Path.cwd() / ".mcp.json"
    mcp_json_path.write_text(json.dumps(mcp_config, indent=2))

    agent_list = ", ".join(f'"{a.name}"' for a in agents)
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
        print(
            f"Warning: Port {api_port} is already in use. API server may fail to start."
        )
    if _is_port_in_use(mcp_port):
        print(
            f"Warning: Port {mcp_port} is already in use. MCP server may fail to start."
        )

    # Ports go via env so create_community_app + the MCP runner read them.
    # MUST be set BEFORE importing okto_pulse.community.main — that module
    # evaluates `app = create_community_app()` at import time, which reads
    # the env vars to inject /config.js with the correct API_URL/MCP_URL.
    os.environ["OKTO_PULSE_PORT"] = str(api_port)
    os.environ["OKTO_PULSE_MCP_PORT"] = str(mcp_port)

    from okto_pulse.community.config import CommunitySettings
    from okto_pulse.community.serve_lock import (
        ServeAlreadyRunningError,
        acquire_serve_lock,
    )

    settings = CommunitySettings()
    frontend_dir = Path(__file__).resolve().parent / "frontend_dist"
    has_frontend = frontend_dir.exists() and (frontend_dir / "index.html").exists()

    try:
        with acquire_serve_lock(settings):
            # Terms-of-Use pre-acceptance via CLI flag or env var.
            if getattr(args, "accept_terms", False):
                os.environ["OKTO_PULSE_TERMS_ACCEPTED"] = "1"
                from okto_pulse.community.acceptance import write_acceptance

                rec = write_acceptance("cli")
                print(
                    f"Terms-of-Use pre-accepted via --accept-terms (version {rec['version']})."
                )
            elif (os.environ.get("OKTO_PULSE_TERMS_ACCEPTED") or "").strip() == "1":
                from okto_pulse.community.acceptance import (
                    write_acceptance,
                    read_acceptance,
                )

                if read_acceptance() is None:
                    rec = write_acceptance("env")
                    print(
                        f"Terms-of-Use pre-accepted via env (version {rec['version']})."
                    )

            print("Starting Okto Pulse Community...")
            if has_frontend:
                print(f"  App:  http://127.0.0.1:{api_port}  (API + Frontend)")
            else:
                print(f"  API:  http://127.0.0.1:{api_port}  (no frontend embedded)")
            print(f"  MCP:  http://127.0.0.1:{mcp_port}/mcp")
            print("  Press Ctrl+C to stop.\n")

            # Single-process, dual-port: run() spawns two uvicorn Server instances
            # via asyncio.gather. uvicorn signal capture is DISABLED for both; main.py handles SIGINT (asyncio.Runner) and installs SIGTERM/SIGBREAK handlers for the ordered shutdown (KGD-01).
            from okto_pulse.community.main import run

            run()
    except ServeAlreadyRunningError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)


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


def cmd_metrics(args):
    """Control metrics On/Off settings and local data."""
    from okto_pulse.community.adapters.telemetry_composition import (
        register_community_telemetry_runtime,
    )
    from okto_pulse.community.config import CommunitySettings
    from okto_pulse.core.telemetry.telemetry_port_registry import get_telemetry_port

    # Compose the same complete vertical used by the server. Partial registration
    # would leave fail-closed effect configuration unresolved for state/target refs.
    register_community_telemetry_runtime()

    settings = CommunitySettings()
    service = get_telemetry_port(settings)
    command = args.metrics_command

    if command == "status":
        print(
            json.dumps(
                service.summary(window_days=args.window_days), indent=2, sort_keys=True
            )
        )
        return

    if command == "enable-beacon":
        if not args.yes:
            print(
                "CONFIRMATION_REQUIRED: pass --yes after reviewing schema and privacy policy.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        acknowledged_items = [
            "hourly_aggregates",
            "local_control",
            "no_pii",
            "product_aggregates",
            "privacy_policy",
            "schema",
        ]
        result = service.update_settings(
            mode="anonymous_beacon",
            source="cli",
            policy_version=args.policy_version,
            schema_version=args.schema_version,
            acknowledged_items=acknowledged_items,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return

    if command in {"local-only", "disable"}:
        if command == "local-only":
            _METRICS_CLI_LOGGER.info(
                "metrics.cli.legacy_local_only",
                extra={
                    "metric_name": "metrics_cli_legacy_local_only_total",
                    "outcome": "mapped_to_disabled",
                },
            )
        result = service.update_settings(mode="disabled", source="cli")
        if command == "local-only":
            result["legacy_alias"] = "local-only"
            result["message"] = (
                "The legacy local-only command is deprecated; metrics are now Off."
            )
        print(json.dumps(result, indent=2, sort_keys=True))
        return

    if command == "export":
        result = service.export_local(args.output)
        print(json.dumps(result, indent=2, sort_keys=True))
        return

    if command == "purge-local":
        if not args.yes:
            print(
                "CONFIRMATION_REQUIRED: pass --yes to purge local metrics files.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        result = service.purge_local()
        print(json.dumps(result, indent=2, sort_keys=True))
        return

    raise SystemExit(f"Unknown metrics command: {command}")


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
        print(
            f"Database not found at {db_path}. Run 'okto-pulse init' first.",
            file=sys.stderr,
        )
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
        print(
            f"Database not initialised: {exc}. Run 'okto-pulse init' first.",
            file=sys.stderr,
        )
        sys.exit(1)
    finally:
        conn.close()

    if row is None or not row[0]:
        print("No bootstrap API key found in database.", file=sys.stderr)
        sys.exit(1)
    if not _is_recoverable_agent_key(row[0]):
        print(
            "Bootstrap API key is reveal-once and is not recoverable from the database.",
            file=sys.stderr,
        )
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
    from okto_pulse.core.ports.relational_runtime import (
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
    from okto_pulse.community.adapters.composition import (
        configure_community_kg_registry,
    )

    board_id: str = args.board_id
    emit_json: bool = bool(getattr(args, "json", False))

    # KGD-01 C6 (S10): check_kuzu abre o grafo do board — falha rápida com
    # servidor vivo.
    _fail_fast_if_server_running("verify-pipeline")

    settings = CommunitySettings()
    configure_settings(settings)
    _configure_community_relational_runtime(settings, echo=False)
    # R01C REPLAN-IMP4: Community owns the schema lifecycle here too — register
    # the orchestrator so this command's init_db delegates to the edition
    # migrator+bootstrapper (idempotent; same lifecycle as serve/init).
    from okto_pulse.community.adapters.relational_schema_lifecycle import (
        register_community_relational_schema_lifecycle,
    )

    register_community_relational_schema_lifecycle()

    async def _run() -> list:
        await init_db()
        factory = get_session_factory()
        configure_community_kg_registry(factory)
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
    from okto_pulse.core.ports.relational_runtime import (
        get_session_factory,
        init_db,
        close_db,
    )
    from okto_pulse.core.services.application_kg import create_deterministic_worker
    from sqlalchemy import text as sa_text

    board_id: str = args.board_id
    apply_writes: bool = bool(getattr(args, "apply", False))
    artifact_filter: str = getattr(args, "artifact_type", "") or ""
    emit_json: bool = bool(getattr(args, "json", False))

    # KGD-01 C6 (S10): o caminho --apply abre e ESCREVE no grafo do board —
    # falha rápida com servidor vivo. O dry-run não toca o Kùzu.
    if apply_writes:
        _fail_fast_if_server_running("kg backfill --apply")

    settings = CommunitySettings()
    configure_settings(settings)
    _configure_community_relational_runtime(settings, echo=False)
    # R01C REPLAN-IMP4: Community owns the schema lifecycle here too — register
    # the orchestrator so both the dry-run and apply (_apply_backfill) init_db
    # calls delegate to the edition migrator+bootstrapper (idempotent).
    from okto_pulse.community.adapters.relational_schema_lifecycle import (
        register_community_relational_schema_lifecycle,
    )

    register_community_relational_schema_lifecycle()

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
                spec_rows = _result_records(
                    await db.execute(
                        sa_text(
                            "SELECT id, title, description, context, "
                            "functional_requirements, technical_requirements, "
                            "acceptance_criteria, test_scenarios, business_rules, "
                            "api_contracts "
                            "FROM specs WHERE board_id = :board_id "
                            "ORDER BY created_at, id"
                        ),
                        {"board_id": board_id},
                    )
                )
                sprint_rows = _result_records(
                    await db.execute(
                        sa_text(
                            "SELECT id, title, description, objective, "
                            "expected_outcome, spec_id "
                            "FROM sprints WHERE board_id = :board_id "
                            "ORDER BY created_at, id"
                        ),
                        {"board_id": board_id},
                    )
                )
                card_rows = _result_records(
                    await db.execute(
                        sa_text(
                            "SELECT id, title, description, card_type, "
                            "origin_task_id, sprint_id, spec_id, priority "
                            "FROM cards WHERE board_id = :board_id "
                            "ORDER BY created_at, id"
                        ),
                        {"board_id": board_id},
                    )
                )
                return {
                    "specs": [_spec_to_dict(s) for s in spec_rows],
                    "sprints": [_sprint_to_dict(s) for s in sprint_rows],
                    "cards": [_card_to_dict(c) for c in card_rows],
                }
        finally:
            await close_db()

    data = asyncio.run(_load())

    worker = create_deterministic_worker()
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
            summary["per_artifact"].append(
                {
                    "artifact_type": art_type,
                    "artifact_id": artifact.get("id"),
                    "error": str(exc),
                }
            )
            continue
        summary["artifacts"][art_type] += 1
        summary["nodes_total"] += len(result.nodes)
        summary["edges_total"] += len(result.edges)
        summary["missing_link_candidates"] += len(result.missing_link_candidates)
        summary["per_artifact"].append(
            {
                "artifact_type": art_type,
                "artifact_id": artifact.get("id"),
                "nodes": len(result.nodes),
                "edges": len(result.edges),
                "missing_link_candidates": len(result.missing_link_candidates),
                "deterministic_edge_ratio": result.deterministic_edge_ratio(),
                "content_hash": result.content_hash,
            }
        )

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
    from okto_pulse.core.ports.relational_runtime import (
        get_session_factory,
        init_db,
        close_db,
    )
    from okto_pulse.community.adapters.composition import (
        configure_community_kg_registry,
    )
    from okto_pulse.core.services.application_kg import (
        get_current_provider_registry,
        start_historical_consolidation,
    )
    from okto_pulse.core.application.processors import ConsolidationProcessor
    from okto_pulse.community.adapters.worker_runners import (
        TrackedBlockingExecution,
        UtcWorkerClock,
    )
    from sqlalchemy import text as sa_text

    await init_db()
    factory = get_session_factory()
    configure_community_kg_registry(factory)

    try:
        # Bootstrap Kùzu graph schema for this board.
        # R05-C: via the #06 GraphSchemaManager port (community registry
        # configured just above) rather than the direct kg.schema symbol.
        await get_current_provider_registry().graph_schema_manager.ensure_bootstrapped(
            board_id
        )

        # Enqueue all artifacts via governance
        async with factory() as db:
            result = await start_historical_consolidation(db, board_id)
        total_queued = result.get("total_artifacts", 0)

        if total_queued == 0 and result.get("status") != "already_in_progress":
            if emit_json:
                print(
                    json.dumps(
                        {
                            "board_id": board_id,
                            "status": "no_artifacts",
                            "total_queued": 0,
                            "total_processed": 0,
                            "failed_count": 0,
                        }
                    )
                )
            else:
                print(f"KG backfill [APPLY] for board {board_id}")
                print(
                    "  No eligible artifacts found (need done/approved specs or closed sprints)"
                )
            return

        # CLI owns execution timing; Core supplies only the processor policy.
        worker = ConsolidationProcessor(
            factory,
            clock=UtcWorkerClock(),
            blocking_execution=TrackedBlockingExecution(),
        )
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
            failed = _result_records(
                await db.execute(
                    sa_text(
                        "SELECT artifact_type, artifact_id, last_error "
                        "FROM consolidation_queue "
                        "WHERE board_id = :board_id AND status = :status "
                        "ORDER BY artifact_type, artifact_id"
                    ),
                    {"board_id": board_id, "status": "failed"},
                )
            )
        failed_count = len(failed)

        if emit_json:
            output = {
                "board_id": board_id,
                "status": "already_in_progress"
                if result.get("status") == "already_in_progress"
                else "completed",
                "total_queued": total_queued,
                "total_processed": total_processed,
                "failed_count": failed_count,
            }
            if failed:
                output["failures"] = [
                    {
                        "artifact_type": _field(f, "artifact_type"),
                        "artifact_id": _field(f, "artifact_id"),
                        "error": _field(f, "error_message")
                        or _field(f, "last_error")
                        or "unknown",
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
                    err = (
                        _field(f, "error_message")
                        or _field(f, "last_error")
                        or "unknown"
                    )
                    print(
                        f"    - {_field(f, 'artifact_type')}/"
                        f"{_field(f, 'artifact_id')}: {err}"
                    )
            else:
                print("  All entries processed successfully")

    finally:
        await close_db()


def _spec_to_dict(s):
    return {
        "id": _field(s, "id"),
        "title": _field(s, "title"),
        "description": _field(s, "description"),
        "context": _field(s, "context"),
        "functional_requirements": _json_field(s, "functional_requirements"),
        "technical_requirements": _json_field(s, "technical_requirements"),
        "acceptance_criteria": _json_field(s, "acceptance_criteria"),
        "test_scenarios": _json_field(s, "test_scenarios"),
        "business_rules": _json_field(s, "business_rules"),
        "api_contracts": _json_field(s, "api_contracts"),
    }


def _sprint_to_dict(s):
    return {
        "id": _field(s, "id"),
        "title": _field(s, "title"),
        "description": _field(s, "description"),
        "objective": _field(s, "objective"),
        "expected_outcome": _field(s, "expected_outcome"),
        "spec_id": _field(s, "spec_id"),
    }


def _card_to_dict(c):
    p = _field(c, "priority")
    card_type = _field(c, "card_type")
    if hasattr(card_type, "value"):
        card_type = card_type.value
    return {
        "id": _field(c, "id"),
        "title": _field(c, "title"),
        "description": _field(c, "description"),
        "card_type": str(card_type) if card_type else "normal",
        "origin_task_id": _field(c, "origin_task_id"),
        "sprint_id": _field(c, "sprint_id"),
        "spec_id": _field(c, "spec_id"),
        "priority": str(p.value)
        if hasattr(p, "value") and p is not None
        else (str(p) if p is not None else None),
    }


def cmd_kg_dedup_entities(args):
    """NC-8 / MKG-C-S1 — consolidate duplicate Kuzu nodes per
    (node_type, source_artifact_ref), reversible by construction.

    The write path requires an explicit confirmation artifact
    (--confirm) and records the complete snapshot in the equivalence
    ledger BEFORE tombstoning the duplicates — no edge re-point, no
    physical delete. --hard-delete is refused by the curation policy
    (forbidden). --dry-run remains a zero-mutation preview. Output is a
    human-readable table by default; --json for ops automation.
    """
    from okto_pulse.community.adapters.composition import (
        configure_community_kg_registry,
    )
    from okto_pulse.community.adapters.relational_schema_lifecycle import (
        register_community_relational_schema_lifecycle,
    )
    from okto_pulse.community.config import CommunitySettings
    from okto_pulse.core.infra.config import configure_settings
    from okto_pulse.core.infra.database import get_session_factory, init_db
    from okto_pulse.core.kg.curation_policy import CurationPolicyError
    from okto_pulse.core.kg.dedup_migration import (
        format_report_table,
        migrate_dedup_entities,
    )

    board_id: str = args.board_id
    dry_run: bool = bool(getattr(args, "dry_run", False))
    emit_json: bool = bool(getattr(args, "json", False))
    confirmed: bool = bool(getattr(args, "confirm", False))
    hard_delete: bool = bool(getattr(args, "hard_delete", False))
    propose: bool = bool(getattr(args, "propose", False))
    approve_id: str = str(getattr(args, "approve", "") or "")

    # KGD-01 C6 (S10): dedup abre o grafo do board (e escreve com --confirm) —
    # falha rápida com servidor vivo (o dry-run também abre para leitura).
    _fail_fast_if_server_running("kg dedup-entities")

    settings = CommunitySettings()
    configure_settings(settings)
    # MKG-C-S1 (FR1): o ledger de equivalência vive no DB relacional — o
    # caminho de escrita precisa do runtime + registry compostos (o mesmo
    # wiring registra o EquivalenceLedger fail-closed).
    _configure_community_relational_runtime(settings, echo=False)
    register_community_relational_schema_lifecycle()

    async def _setup() -> None:
        await init_db()
        configure_community_kg_registry(get_session_factory())

    asyncio.run(_setup())

    try:
        if propose:
            from okto_pulse.core.kg.dedup_migration import (
                propose_dedup_entities,
            )

            report = propose_dedup_entities(board_id)
        elif approve_id:
            from okto_pulse.core.kg.dedup_migration import (
                StaleProposalError,
                approve_dedup_proposal,
            )

            try:
                report = approve_dedup_proposal(board_id, approve_id)
            except StaleProposalError as exc:
                payload = {
                    "error": exc.code,
                    "proposal_id": exc.proposal_id,
                    "expected_hash": exc.expected_hash,
                    "current_hash": exc.current_hash,
                    "remediation": "Estado do grafo mudou desde a proposta;"
                    " re-execute --propose e aprove o novo plano.",
                }
                if emit_json:
                    print(json.dumps(payload, indent=2))
                else:
                    print(f"ERRO {exc.code}: {exc}")
                sys.exit(3)
        else:
            report = migrate_dedup_entities(
                board_id,
                dry_run=dry_run,
                confirmed=confirmed,
                hard_delete=hard_delete,
            )
    except CurationPolicyError as exc:
        payload = {
            "error": exc.code,
            "operation": exc.operation,
            "level": exc.level,
            "remediation": exc.remediation,
        }
        if emit_json:
            print(json.dumps(payload, indent=2))
        else:
            print(
                f"ERRO {exc.code}: operacao {exc.operation} "
                f"(nivel {exc.level}).\n{exc.remediation}"
            )
        sys.exit(2)

    if emit_json or propose:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(format_report_table(report))
    sys.exit(0)


def cmd_kg_unmerge(args):
    """MKG-C-S1 (FR4/BR3) — logically reverse a dedup merge.

    Clears the tombstones the record created and revokes the ledger
    record (preserved for audit). Never re-points edges. Idempotent on an
    already-revoked record.
    """
    from okto_pulse.community.adapters.composition import (
        configure_community_kg_registry,
    )
    from okto_pulse.community.adapters.relational_schema_lifecycle import (
        register_community_relational_schema_lifecycle,
    )
    from okto_pulse.community.config import CommunitySettings
    from okto_pulse.core.infra.config import configure_settings
    from okto_pulse.core.infra.database import get_session_factory, init_db
    from okto_pulse.core.kg.dedup_migration import unmerge_equivalence
    from okto_pulse.core.ports.kg_equivalence_ledger import (
        EquivalenceLedgerError,
    )

    board_id: str = args.board_id
    record_id: str = args.record_id
    emit_json: bool = bool(getattr(args, "json", False))

    # KGD-01 C6 / MKG-C D6: un-merge writes to the board graph — single
    # writer guard before anything opens.
    _fail_fast_if_server_running("kg unmerge")

    settings = CommunitySettings()
    configure_settings(settings)
    _configure_community_relational_runtime(settings, echo=False)
    register_community_relational_schema_lifecycle()

    async def _setup() -> None:
        await init_db()
        configure_community_kg_registry(get_session_factory())

    asyncio.run(_setup())

    try:
        result = unmerge_equivalence(board_id, record_id)
    except EquivalenceLedgerError as exc:
        payload = {
            "error": exc.failure_reason,
            "record_id": exc.record_id,
            "remediation": exc.remediation,
        }
        if emit_json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"ERRO {exc.failure_reason}: {exc.remediation or exc}")
        sys.exit(2)

    if emit_json:
        print(json.dumps(result, indent=2, default=str))
    elif result.get("already_revoked"):
        print(
            f"AVISO: registro {record_id} ja estava revogado — no-op "
            f"idempotente."
        )
    else:
        print(
            f"Un-merge concluido: {result['members_restored']} membro(s) "
            f"restaurado(s) do survivor {result.get('survivor_id')} "
            f"(registro {record_id} revogado, preservado para auditoria)."
        )
    sys.exit(0)


def cmd_kg_proposals(args):
    """MKG-C-S1 (FR7) — list pending curation proposals for a board."""
    from okto_pulse.community.adapters.composition import (
        configure_community_kg_registry,
    )
    from okto_pulse.community.adapters.relational_schema_lifecycle import (
        register_community_relational_schema_lifecycle,
    )
    from okto_pulse.community.config import CommunitySettings
    from okto_pulse.core.infra.config import configure_settings
    from okto_pulse.core.infra.database import get_session_factory, init_db
    from okto_pulse.core.ports.kg_curation_proposals import (
        require_curation_proposal_store,
    )

    board_id: str = args.board_id
    emit_json: bool = bool(getattr(args, "json", False))

    settings = CommunitySettings()
    configure_settings(settings)
    _configure_community_relational_runtime(settings, echo=False)
    register_community_relational_schema_lifecycle()

    async def _run() -> tuple:
        await init_db()
        configure_community_kg_registry(get_session_factory())
        store = require_curation_proposal_store()
        return await store.pending_for_board(board_id)

    proposals = asyncio.run(_run())
    if emit_json:
        print(json.dumps(
            [
                {
                    "proposal_id": pr.proposal_id,
                    "operation": pr.operation,
                    "proposal_hash": pr.proposal_hash,
                    "created_at": pr.created_at,
                    "groups": len(dict(pr.plan).get("groups", [])),
                }
                for pr in proposals
            ],
            indent=2,
        ))
    elif not proposals:
        print(f"Nenhuma proposta pendente para o board {board_id}.")
    else:
        for pr in proposals:
            print(
                f"{pr.proposal_id}  {pr.operation}  "
                f"groups={len(dict(pr.plan).get('groups', []))}  "
                f"hash={pr.proposal_hash[:12]}  {pr.created_at}"
            )
    sys.exit(0)


def cmd_kg_restore(args):
    """KGD-01 FR4 — `okto-pulse kg restore <quarantine_id> [--apply]`.

    Dry-run (default) prints the auditable restore plan (files, destinations,
    conflicts, sizes) with ZERO mutation. `--apply` performs the backup-swap:
    the board's live files move into a NEW quarantine with manifest, the
    snapshot is copied back and the board open is validated. The adapter
    refuses `--apply` while a live server holds the data dir (serve-lock →
    structured `board_locked`); mid-flight failures return `partial_restore`
    with the operation manifest recording the exact state for rollback.
    """
    from okto_pulse.community.adapters.quarantine_restore import (
        CommunityQuarantineRestore,
    )
    from okto_pulse.core.kg.interfaces.quarantine_restore import (
        QuarantineRestoreError,
    )
    from okto_pulse.core.infra.config import get_settings

    quarantine_id: str = args.quarantine_id
    apply_restore: bool = bool(getattr(args, "apply", False))
    emit_json: bool = bool(getattr(args, "json", False))

    extra_lock_dirs: tuple[Path, ...] = ()
    try:
        from okto_pulse.community.config import CommunitySettings

        data_dir = getattr(CommunitySettings(), "data_dir", None)
        if data_dir:
            extra_lock_dirs = (Path(data_dir).expanduser(),)
    except Exception:
        extra_lock_dirs = ()

    service = CommunityQuarantineRestore(
        base_dir=get_settings().kg_base_dir,
        extra_serve_lock_dirs=extra_lock_dirs,
    )

    def _emit_restore_error(exc: QuarantineRestoreError) -> None:
        payload = exc.to_payload()
        if emit_json:
            print(json.dumps(payload, indent=2, default=str))
        else:
            print(f"ERROR [{payload['error']}]: {payload['detail']}")
            for key, value in (payload.get("details") or {}).items():
                print(f"  {key}: {value}")
        sys.exit(2)

    try:
        plan = service.plan(quarantine_id)
    except QuarantineRestoreError as exc:
        _emit_restore_error(exc)
        return

    if not apply_restore:
        if emit_json:
            print(
                json.dumps(
                    {
                        "plan": plan.to_payload(),
                        "applied": False,
                        "quarantine_id": plan.quarantine_id,
                        "board_id": plan.board_id,
                        "board_dir": plan.board_dir,
                        "conflicts": list(plan.conflicts),
                        "total_bytes": plan.total_bytes,
                    },
                    indent=2,
                    default=str,
                )
            )
        else:
            print(f"KG quarantine restore [DRY-RUN] {plan.quarantine_id}")
            print(f"  Board:       {plan.board_id}")
            print(f"  Destination: {plan.board_dir}")
            print(f"  Manifest:    {plan.manifest_format}")
            print(f"  Total bytes: {plan.total_bytes}")
            for entry in plan.files:
                marker = "CONFLICT" if entry.conflict else "ok"
                print(
                    f"    {entry.name:<24} {entry.size_bytes:>12}B "
                    f"-> {entry.destination_path} [{marker}]"
                )
            print("  No files were modified (dry-run). Use --apply to restore.")
        sys.exit(0)

    try:
        report = service.apply(quarantine_id)
    except QuarantineRestoreError as exc:
        _emit_restore_error(exc)
        return

    if emit_json:
        print(
            json.dumps(
                {
                    "plan": plan.to_payload(),
                    "applied": report.applied,
                    "backup_quarantine_id": report.backup_quarantine_id,
                    "quarantine_id": report.quarantine_id,
                    "board_id": report.board_id,
                    "restored_files": list(report.restored_files),
                    "open_validated": report.open_validated,
                    "errors": list(report.errors),
                },
                indent=2,
                default=str,
            )
        )
    else:
        print(f"KG quarantine restore [APPLIED] {report.quarantine_id}")
        print(f"  Board:                {report.board_id}")
        print(f"  Backup quarantine:    {report.backup_quarantine_id}")
        print(f"  Restored files:       {', '.join(report.restored_files)}")
        print(f"  Open validated:       {report.open_validated}")
        for err in report.errors:
            print(f"  WARNING: {err}")
    sys.exit(0 if report.open_validated else 3)


def cmd_reset(args):
    """Reset all data — delete DB and uploads, re-seed."""
    from okto_pulse.community.config import CommunitySettings

    settings = CommunitySettings()
    data_path = Path(settings.data_dir)
    uploads_path = data_path / "uploads"

    if not args.yes:
        confirm = input(
            f"This will DELETE all data in {data_path}. Are you sure? [y/N] "
        )
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
    raw_argv = list(sys.argv[1:])
    metrics_legacy_local_only = (
        len(raw_argv) >= 2 and raw_argv[0] == "metrics" and raw_argv[1] == "local-only"
    )
    if metrics_legacy_local_only:
        raw_argv = ["metrics", "disable", *raw_argv[2:]]

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
    sub_init = subparsers.add_parser(
        "init", help="Initialize data directory and seed database"
    )
    sub_init.add_argument(
        "--agents",
        nargs="*",
        metavar="NAME",
        help="Export specific agents to .mcp.json (comma-separated names, or all if empty)",
    )
    sub_init.set_defaults(func=cmd_init)

    # serve
    sub_serve = subparsers.add_parser(
        "serve", help="Start API + Frontend + MCP servers"
    )
    sub_serve.add_argument(
        "--api-port",
        type=int,
        default=DEFAULT_API_PORT,
        help=f"API + Frontend server port (default: {DEFAULT_API_PORT})",
    )
    sub_serve.add_argument(
        "--mcp-port",
        type=int,
        default=DEFAULT_MCP_PORT,
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
    sub_status = subparsers.add_parser(
        "status", help="Show service status and DB metrics"
    )
    sub_status.add_argument(
        "--api-port",
        type=int,
        default=DEFAULT_API_PORT,
        help=f"API server port (default: {DEFAULT_API_PORT})",
    )
    sub_status.add_argument(
        "--mcp-port",
        type=int,
        default=DEFAULT_MCP_PORT,
        help=f"MCP server port (default: {DEFAULT_MCP_PORT})",
    )
    sub_status.set_defaults(func=cmd_status)

    # metrics
    sub_metrics = subparsers.add_parser(
        "metrics",
        help="Control metrics On/Off, export, and purge",
    )
    metrics_sub = sub_metrics.add_subparsers(
        dest="metrics_command", help="Metrics commands"
    )

    metrics_status = metrics_sub.add_parser("status", help="Show metrics status")
    metrics_status.add_argument("--window-days", type=int, default=30)
    metrics_status.set_defaults(func=cmd_metrics)

    metrics_enable = metrics_sub.add_parser(
        "enable-beacon", help="Turn metrics On with anonymous hourly aggregates"
    )
    metrics_enable.add_argument("--policy-version", required=True)
    from okto_pulse.core.telemetry.schema import CURRENT_SCHEMA_VERSION

    metrics_enable.add_argument("--schema-version", default=CURRENT_SCHEMA_VERSION)
    metrics_enable.add_argument(
        "--yes", action="store_true", help="Confirm opt-in prerequisites"
    )
    metrics_enable.set_defaults(func=cmd_metrics)

    metrics_disable = metrics_sub.add_parser("disable", help="Turn metrics Off")
    metrics_disable.set_defaults(func=cmd_metrics)

    metrics_export = metrics_sub.add_parser("export", help="Export local metrics JSONL")
    metrics_export.add_argument("--output")
    metrics_export.set_defaults(func=cmd_metrics)

    metrics_purge = metrics_sub.add_parser(
        "purge-local", help="Purge local metrics files"
    )
    metrics_purge.add_argument("--yes", action="store_true", help="Confirm local purge")
    metrics_purge.set_defaults(func=cmd_metrics)

    # api-key — print bootstrap dash_<hex> from the seeded DB.
    sub_apikey = subparsers.add_parser(
        "api-key",
        help="Print the bootstrap API key (dash_<hex>) seeded by 'okto-pulse init'",
    )
    sub_apikey.set_defaults(func=cmd_api_key)

    # reset
    sub_reset = subparsers.add_parser("reset", help="Delete all data and re-seed")
    sub_reset.add_argument(
        "-y", "--yes", action="store_true", help="Skip confirmation prompt"
    )
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
        "--apply",
        action="store_true",
        help="Apply writes to Kùzu (default: dry-run diff only)",
    )
    sub_backfill.add_argument(
        "--artifact-type",
        default="",
        choices=("", "spec", "sprint", "card"),
        help="Limit to one artifact type (default: all)",
    )
    sub_backfill.add_argument(
        "--json",
        action="store_true",
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
        "--dry-run",
        action="store_true",
        help="Report duplicates without modifying the graph",
    )
    sub_dedup.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of table",
    )
    sub_dedup.add_argument(
        "--confirm",
        action="store_true",
        help="Confirmation artifact required by the curation policy "
        "(propose_only): tombstone the duplicates and record the "
        "equivalence ledger snapshot",
    )
    sub_dedup.add_argument(
        "--hard-delete",
        action="store_true",
        help="Legacy physical delete + edge re-point — REFUSED by the "
        "curation policy (forbidden); physical materialization happens "
        "only inside the deterministic rebuild",
    )
    sub_dedup.add_argument(
        "--propose",
        action="store_true",
        help="Persist a curation proposal (canonical plan + hash) without "
        "mutating anything; approve later with --approve",
    )
    sub_dedup.add_argument(
        "--approve",
        default="",
        metavar="PROPOSAL_ID",
        help="Execute a pending proposal after re-validating its hash "
        "against the current graph state (stale_proposal refuses)",
    )
    sub_dedup.set_defaults(func=cmd_kg_dedup_entities)

    # MKG-C-S1 (FR7) — pending curation proposals
    sub_proposals = kg_subparsers.add_parser(
        "proposals",
        help="List pending curation proposals for a board",
    )
    sub_proposals.add_argument("board_id", help="Target board UUID")
    sub_proposals.add_argument(
        "--json", action="store_true",
        help="Emit machine-readable JSON instead of text",
    )
    sub_proposals.set_defaults(func=cmd_kg_proposals)

    # MKG-C-S1 (FR4) — logical un-merge of a dedup equivalence record
    sub_unmerge = kg_subparsers.add_parser(
        "unmerge",
        help="Reverse a dedup merge: de-tombstone members and revoke the "
        "equivalence ledger record (never re-points edges)",
    )
    sub_unmerge.add_argument("board_id", help="Target board UUID")
    sub_unmerge.add_argument(
        "record_id", help="Equivalence ledger record id (eqv_...)"
    )
    sub_unmerge.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of text",
    )
    sub_unmerge.set_defaults(func=cmd_kg_unmerge)

    # KGD-01 FR4 — quarantine restore (dry-run by default; --apply mutates)
    sub_restore = kg_subparsers.add_parser(
        "restore",
        help="Restore a KG quarantine snapshot to its board (dry-run by default)",
    )
    sub_restore.add_argument(
        "quarantine_id",
        help="Quarantine ID (directory name under <kg_base>/quarantine/)",
    )
    sub_restore.add_argument(
        "--apply",
        action="store_true",
        help="Apply the restore: backup-swap live board files into a new "
        "quarantine, copy the snapshot back and validate the open "
        "(default: dry-run plan only; refused while a server is running)",
    )
    sub_restore.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of table",
    )
    sub_restore.set_defaults(func=cmd_kg_restore)

    args = parser.parse_args(raw_argv)
    if metrics_legacy_local_only:
        args.metrics_command = "local-only"
    if not args.command:
        _print_banner()
        parser.print_help()
        sys.exit(1)
    if args.command == "kg" and not getattr(args, "kg_command", None):
        _print_banner()
        sub_kg.print_help()
        sys.exit(1)
    if args.command == "metrics" and not getattr(args, "metrics_command", None):
        _print_banner()
        sub_metrics.print_help()
        sys.exit(1)

    _print_banner()
    args.func(args)


if __name__ == "__main__":
    main()
