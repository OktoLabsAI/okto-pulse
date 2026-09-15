import logging

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters.sqlite_writer_diagnostics import (
    install_sqlite_writer_diagnostics,
)


@pytest.mark.asyncio
async def test_identifies_writer_on_busy_without_logging_payload(tmp_path, caplog):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'writer.db'}",
        connect_args={"timeout": 0.01},
    )
    install_sqlite_writer_diagnostics(engine)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE probe(value TEXT)"))
        with caplog.at_level(logging.INFO):
            async with engine.connect() as writer, engine.connect() as contender:
                await writer.execute(text("INSERT INTO probe VALUES (:value)"),
                                     {"value": "PRIVATE_PAYLOAD_NOT_FOR_LOGS"})
                with pytest.raises(OperationalError):
                    await contender.execute(text("INSERT INTO probe VALUES ('other')"))
                assert "db.sqlite.lock_failure" in caplog.text
                assert "tracked_writers=[(" in caplog.text
                assert "PRIVATE_PAYLOAD_NOT_FOR_LOGS" not in caplog.text
                await writer.rollback()
                await contender.rollback()
                await contender.execute(text("INSERT INTO probe VALUES ('after')"))
                await contender.commit()
                assert "writer_end_requested" in caplog.text
    finally:
        await engine.dispose()
