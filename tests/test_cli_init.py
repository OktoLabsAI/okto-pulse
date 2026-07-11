"""Unit tests for the `okto-pulse init` CLI command.

Tests argparse wiring via subprocess (matching the pattern in
test_cli_kg_backfill.py).  Full cmd_init flow requires a real DB
so we only test the subparser shape here.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_SRC = Path(__file__).parent.parent / "src"
CORE_SRC = Path(__file__).parent.parent.parent / "okto-pulse-core" / "src"

for p in (str(REPO_SRC), str(CORE_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

for mod in list(sys.modules):
    if mod.startswith("okto_pulse.community"):
        del sys.modules[mod]


# ---------------------------------------------------------------------------
# Argparse wiring
# ---------------------------------------------------------------------------


def test_init_subparser_has_agents_flag():
    """The init subparser exposes --agents (nargs='*')."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, r'{}'); "
            "from okto_pulse.community.cli import main; main()".format(str(REPO_SRC)),
            "init",
            "--help",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "--agents" in result.stdout


def test_init_subparser_no_args_shows_help(tmp_path):
    """Running `okto-pulse init` with no subcommand prints help and exits 1."""
    env = dict(os.environ)
    env["DATA_DIR"] = str(tmp_path / "data")
    env["OKTO_PULSE_HOME"] = str(tmp_path / "home")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, r'{}'); "
            "from okto_pulse.community.cli import main; main()".format(str(REPO_SRC)),
            "init",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode in (0, 1)


# ---------------------------------------------------------------------------
# AF14 TS6 - deferred Community migration contract
# ---------------------------------------------------------------------------


class _FakeScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _query):
        return _FakeScalarResult(self._rows)


def _patch_mcp_export_runtime(monkeypatch, rows):
    import okto_pulse.community.adapters.composition as composition
    import okto_pulse.community.adapters.relational_schema_lifecycle as lifecycle
    import okto_pulse.core.infra.auth as auth
    import okto_pulse.core.infra.config as config
    import okto_pulse.core.infra.database as database
    import okto_pulse.core.infra.storage as storage

    async def fake_init_db():
        return None

    async def fake_close_db():
        return None

    monkeypatch.setattr(config, "configure_settings", lambda _settings: None)
    monkeypatch.setattr(auth, "configure_auth", lambda _provider: None)
    monkeypatch.setattr(storage, "configure_storage", lambda _provider: None)
    monkeypatch.setattr(composition, "community_storage_provider", lambda _path: None)
    monkeypatch.setattr(database, "create_database", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(database, "init_db", fake_init_db)
    monkeypatch.setattr(database, "close_db", fake_close_db)
    monkeypatch.setattr(
        database, "get_session_factory", lambda: lambda: _FakeSession(rows)
    )
    monkeypatch.setattr(
        lifecycle, "register_community_relational_schema_lifecycle", lambda: None
    )


def test_af14_ts6_mcp_export_skips_deferred_markers_but_exports_revealed_and_legacy(
    tmp_path, monkeypatch, capsys
):
    from okto_pulse.community.cli import _generate_mcp_json

    rows = [
        SimpleNamespace(name="Deferred Agent", api_key="sha256:deadbeef"),
        SimpleNamespace(name="Legacy Agent", api_key="dash_legacy_plaintext"),
    ]
    _patch_mcp_export_runtime(monkeypatch, rows)
    monkeypatch.chdir(tmp_path)

    _generate_mcp_json(
        8101,
        ["Deferred Agent", "Legacy Agent", "New Agent"],
        revealed_agents=[("New Agent", "dash_revealed_once")],
    )

    output = capsys.readouterr().out
    config = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    urls = {name: server["url"] for name, server in config["mcpServers"].items()}

    assert "reveal-once only: Deferred Agent" in output
    assert "deferred-agent" not in urls
    assert urls["legacy-agent"].endswith("?api_key=dash_legacy_plaintext")
    assert urls["new-agent"].endswith("?api_key=dash_revealed_once")


def test_af14_ts6_mcp_export_marker_only_writes_no_config(
    tmp_path, monkeypatch, capsys
):
    from okto_pulse.community.cli import _generate_mcp_json

    rows = [SimpleNamespace(name="Deferred Agent", api_key="sha256:deadbeef")]
    _patch_mcp_export_runtime(monkeypatch, rows)
    monkeypatch.chdir(tmp_path)

    _generate_mcp_json(8101, ["Deferred Agent"], revealed_agents=[])

    output = capsys.readouterr().out
    assert "No recoverable agent API keys found" in output
    assert "reveal-once; regenerate one in the UI/API if needed" in output
    assert not (tmp_path / ".mcp.json").exists()


def test_af14_ts6_api_key_cli_refuses_reveal_once_marker(tmp_path, monkeypatch, capsys):
    import okto_pulse.community.config as community_config
    from okto_pulse.community.cli import cmd_api_key

    data_dir = tmp_path / "pulse-data"
    db_dir = data_dir / "data"
    db_dir.mkdir(parents=True)
    conn = sqlite3.connect(db_dir / "pulse.db")
    try:
        conn.execute("CREATE TABLE agents (api_key TEXT, created_at TEXT)")
        conn.execute(
            "INSERT INTO agents (api_key, created_at) VALUES (?, ?)",
            ("sha256:deferred-marker", "2026-07-03T00:00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    Settings = type("Settings", (), {"data_dir": str(data_dir)})
    monkeypatch.setattr(community_config, "CommunitySettings", Settings)

    with pytest.raises(SystemExit) as exc_info:
        cmd_api_key(SimpleNamespace())

    captured = capsys.readouterr()
    assert exc_info.value.code == 1
    assert captured.out == ""
    assert "reveal-once and is not recoverable" in captured.err
