"""Community CLI command for local graph schema migration."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any


async def _compose_and_list_boards(*, list_all: bool) -> list[tuple[str, str]]:
    from sqlalchemy import select

    from okto_pulse.community.adapters.composition import (
        configure_community_kg_registry,
    )
    from okto_pulse.community.adapters.relational_schema_lifecycle import (
        register_community_relational_schema_lifecycle,
    )
    from okto_pulse.community.adapters.sqlalchemy_database import (
        configure_community_database,
    )
    from okto_pulse.community.adapters.sqlalchemy_models import Board
    from okto_pulse.community.config import CommunitySettings
    from okto_pulse.core import configure_settings
    from okto_pulse.community.adapters.sqlalchemy_database import get_session_factory, init_db

    settings = CommunitySettings()
    configure_settings(settings)
    configure_community_database(settings.database_url, echo=False)
    register_community_relational_schema_lifecycle()
    await init_db()
    factory = get_session_factory()
    configure_community_kg_registry(factory)
    if not list_all:
        return []
    async with factory() as session:
        rows = (await session.execute(select(Board.id, Board.name))).all()
    return [(str(row[0]), str(row[1])) for row in rows]


def _run_single_board(board_id: str) -> dict[str, Any]:
    from okto_pulse.core.services.application_kg import migrate_board_graph_schema

    try:
        return asyncio.run(migrate_board_graph_schema(board_id))
    except Exception as exc:
        return {
            "board_id": board_id,
            "migrated": False,
            "columns_added": {},
            "errors": [f"{type(exc).__name__}: {exc}"],
            "duration_ms": 0,
        }


def _emit_single(summary: dict[str, Any]) -> int:
    print(json.dumps(summary, indent=2, default=str))
    return 0 if summary.get("migrated") and not summary.get("errors") else 1


def _emit_all(results: list[dict[str, Any]], names: dict[str, str]) -> int:
    failed = 0
    for result in results:
        board_id = result["board_id"]
        print(
            f"{board_id} ({names.get(board_id, '?')}): "
            f"migrated={result['migrated']} "
            f"columns_added_count="
            f"{sum(len(value) for value in result['columns_added'].values())} "
            f"errors={len(result['errors'])} duration={result['duration_ms']}ms"
        )
        for error in result["errors"]:
            print(f"  ERROR: {error}", file=sys.stderr)
        failed += int(bool(result["errors"]))
    return 0 if failed == 0 else 1


def run(args: argparse.Namespace) -> int:
    """Execute a parsed ``okto-pulse kg migrate-schema`` command."""

    from okto_pulse.community.serve_lock import assert_no_live_server
    from okto_pulse.community.config import CommunitySettings

    assert_no_live_server(
        CommunitySettings().data_dir,
        operation="kg migrate-schema",
    )
    pairs = asyncio.run(
        _compose_and_list_boards(list_all=bool(getattr(args, "all_boards", False)))
    )
    if getattr(args, "all_boards", False):
        names = dict(pairs)
        return _emit_all(
            [_run_single_board(board_id) for board_id, _ in pairs],
            names,
        )
    return _emit_single(_run_single_board(str(args.board_id)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="okto-pulse kg migrate-schema",
        description="Apply local graph schema migrations (idempotent).",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--board", dest="board_id", help="Board UUID to migrate")
    group.add_argument("--all-boards", action="store_true")
    return run(parser.parse_args(argv))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["main", "run"]
