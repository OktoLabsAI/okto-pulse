"""Use a frozen DB created by the byte-verified, installed local v0.3.4 pair.

Do not regenerate this fixture from current metadata. Its missing destination
columns/tables are part of the predecessor contract the upgrade must accept.
"""

import hashlib
import json
from pathlib import Path
import sqlite3
import zipfile

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters.historical_archive_grant_installation import install_historical_archive_grants
from okto_pulse.community.adapters.sprint_retirement_archive import capture_sprint_retirement_archive, verify_historical_archive
from okto_pulse.community.adapters.storage import CommunityFileSystemStorage


FIXTURES = Path(__file__).parent / "fixtures"
SOURCE_ZIP_SHA256 = "ece38af238a07dac2b7af1fac3b35b3c5857a400b71e714a0435809e4424da9a"


def restore_source(tmp_path):
    metadata = json.loads((FIXTURES / "f2_v034_source.json").read_text(encoding="utf-8"))
    path = FIXTURES / "f2_v034_source.sqlite3.zip"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == metadata["zip_sha256"] == SOURCE_ZIP_SHA256
    assert metadata["source_builds"]["core"]["commit"] == "207072509a282e8adec1481d05aee2d54c382bde"
    assert metadata["source_builds"]["community"]["commit"] == "b6dda64f512920fa4aaaf9d50c331b1796b87e27"
    with zipfile.ZipFile(path) as archive:
        assert archive.namelist() == ["source.sqlite3"]
        assert archive.getinfo("source.sqlite3").file_size == metadata["database_size"] <= 8 * 1024 * 1024
        content = archive.read("source.sqlite3")
    assert hashlib.sha256(content).hexdigest() == metadata["database_sha256"]
    destination = tmp_path / "source.sqlite3"
    destination.write_bytes(content)
    return destination


def source_cells(path, names=None):
    with sqlite3.connect(path) as connection:
        if names is None:
            names = [row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")]
        return {name: sorted(connection.execute('SELECT * FROM "' + name.replace('"', '""') + '"').fetchall(), key=repr)
            for name in names}


@pytest.mark.asyncio
async def test_original_source_archives_and_installs_grants_without_bootstrapping_policy(tmp_path):
    path = restore_source(tmp_path)
    before = source_cells(path)
    assert len(before) == 174 and "historical_archive_grants" not in before
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    storage = CommunityFileSystemStorage(str(tmp_path / "storage"))
    try:
        references = await capture_sprint_retirement_archive(engine, storage, migration_id="real-source")
        assert len(references) == 1
        reference = references[0]
        document = await verify_historical_archive(storage, reference)
        grants = document["access"]["grants"]
        assert {grant["scope"]["origin_id"] for grant in grants} == {"sprint-a", "sprint-b"}
        # The source Board belongs to 'owner', not the local authenticated user.
        # A fresh grant destination cannot widen the captured membership ceiling.
        assert all(not any(grant["sections"].values()) for grant in grants)
        assert await install_historical_archive_grants(engine, storage, reference) == len(grants)
        assert await install_historical_archive_grants(engine, storage, reference) == len(grants)
        assert await capture_sprint_retirement_archive(engine, storage, migration_id="real-source") == references
        after = source_cells(path, before)
        assert all(after[name] == rows for name, rows in before.items() if name != "domain_events")
        assert len(after["domain_events"]) == len(before["domain_events"]) + 2
        with sqlite3.connect(path) as connection:
            for table in ("agents", "agent_boards", "permission_presets"):
                assert "permission_migration_review" not in {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
            assert "migrated_validation_policy" not in {row[1] for row in connection.execute('PRAGMA table_info("cards")')}
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        await engine.dispose()
