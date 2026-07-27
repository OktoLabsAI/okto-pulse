"""Community CLI for idempotent legacy FR/AC materialization."""

from __future__ import annotations

import argparse
import asyncio

from okto_pulse.community.adapters.board_source_reader import resolve_pulse_db_path
from okto_pulse.community.adapters.sqlalchemy_database import (
    build_community_engine,
    build_community_session_factory,
)
from okto_pulse.community.adapters.sqlalchemy_spec_materialization import (
    CommunitySqlAlchemySpecMaterializationStore,
)
from okto_pulse.core.application.spec_materialization import (
    materialize_legacy_fr_ac_board,
)


async def _run(board_id: str, *, dry_run: bool) -> dict[str, object]:
    path = resolve_pulse_db_path()
    engine = build_community_engine(f"sqlite+aiosqlite:///{path}")
    session_factory = build_community_session_factory(engine)
    try:
        async with session_factory() as session:
            return await materialize_legacy_fr_ac_board(
                CommunitySqlAlchemySpecMaterializationStore(session),
                board_id,
                dry_run=dry_run,
            )
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Canonicalize legacy string FR/AC into structured entities."
    )
    parser.add_argument("--board-id", required=True, help="Board UUID to migrate.")
    parser.add_argument(
        "--dry-run",
        default="true",
        help="'true' only reports; 'false' persists changes.",
    )
    args = parser.parse_args()
    dry_run = str(args.dry_run).strip().lower() not in ("false", "0", "no")
    print(asyncio.run(_run(args.board_id, dry_run=dry_run)))


if __name__ == "__main__":
    main()
