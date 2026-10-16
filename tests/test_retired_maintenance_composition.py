"""F4: no retired writer in composition; historical SQL remains untouched."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import importlib.util
import os
import sqlite3
import subprocess
import sys

import pytest
from sqlalchemy import create_engine

from okto_pulse.core import configure_settings
from okto_pulse.core.runtime_context import (
    RuntimeValueRegistry,
    resolve_runtime_value,
    runtime_value_scope,
)
from okto_pulse.community.adapters.composition import configure_community_kg_registry
from okto_pulse.community.adapters.sqlalchemy_models import KGCurationProposal
from okto_pulse.community.config import CommunitySettings


@pytest.mark.parametrize("module", [
    "okto_pulse.community.commands.materialize_legacy_fr_ac",
    "okto_pulse.community.adapters.sqlalchemy_spec_materialization",
    "okto_pulse.community.adapters.sqlalchemy_kg_curation_proposals",
])
def test_retired_maintenance_modules_are_absent(module):
    assert importlib.util.find_spec(module) is None


def test_composition_preserves_historical_proposals_without_registering_writer(tmp_path):
    path = tmp_path / "history.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        KGCurationProposal.__table__.create(engine)
        with engine.begin() as connection:
            connection.execute(KGCurationProposal.__table__.insert(), [
                {
                    "proposal_id": f"historical-{status}",
                    "board_id": board_id,
                    "operation": "dedup_entities",
                    "plan": {"groups": [{"id": "old-node", "text": "história"}]},
                    "proposal_hash": "d" * 64,
                    "created_by": "original-agent",
                    "created_at": datetime(2025, 1, 2, tzinfo=timezone.utc),
                    "status": status,
                    "resolved_at": None,
                }
                for status, board_id in (("pending", "board-a"), ("resolved", "board-b"))
            ])
    finally:
        engine.dispose()
    before = path.read_bytes()

    def forbidden_session():
        pytest.fail("registry composition opened a relational session")

    settings = CommunitySettings(
        data_dir=str(tmp_path),
        database_url=f"sqlite+aiosqlite:///{path.as_posix()}",
        kg_base_dir=str(tmp_path / "kg"),
        kg_embedding_mode="stub",
    )
    with runtime_value_scope(RuntimeValueRegistry()):
        configure_settings(settings)
        for _ in range(2):
            configure_community_kg_registry(
                forbidden_session, settings=settings,
            )
            assert resolve_runtime_value("ports.kg.curation_proposal_store") is None

    assert path.read_bytes() == before
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        assert connection.execute(
            "SELECT proposal_id, status, created_by FROM kg_curation_proposals "
            "ORDER BY proposal_id"
        ).fetchall() == [
            ("historical-pending", "pending", "original-agent"),
            ("historical-resolved", "resolved", "original-agent"),
        ]


@pytest.mark.parametrize("arguments", [
    ["--help"],
    ["--board-id", "board-fixture", "--dry-run", "true"],
    ["--board-id", "board-fixture", "--dry-run", "false"],
])
def test_installed_legacy_materializer_module_is_unavailable_before_data_access(
    tmp_path, arguments
):
    path = tmp_path / "data" / "pulse.db"
    path.parent.mkdir()
    path.write_bytes(b"opaque existing database: never open it for this invocation")
    before = {p.relative_to(tmp_path): (p.read_bytes(), p.stat().st_mtime_ns)
              for p in tmp_path.rglob("*") if p.is_file()}
    env = os.environ.copy()
    for key in ("PYTHONPATH", "DATABASE_URL"):
        env.pop(key, None)
    env.update(DATA_DIR=str(tmp_path), KG_BASE_DIR=str(tmp_path / "graph"))
    result = subprocess.run(
        [sys.executable, "-m", "okto_pulse.community.commands.materialize_legacy_fr_ac",
         *arguments],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 1
    assert "No module named okto_pulse.community.commands.materialize_legacy_fr_ac" in result.stderr
    assert not result.stdout
    assert {p.relative_to(tmp_path): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in tmp_path.rglob("*") if p.is_file()} == before
    assert not (tmp_path / "graph").exists()
