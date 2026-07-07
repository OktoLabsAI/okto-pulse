from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_SRC = Path(__file__).parent.parent / "src"
CORE_SRC = Path(__file__).parent.parent.parent / "okto-pulse-core" / "src"

for p in (str(REPO_SRC), str(CORE_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)


def test_hnd2_credential_surface_gate_real_tree_allows_only_governed_surfaces():
    from okto_pulse.community.adapters.credential_surface_gate import (
        run_community_credential_surface_gate,
    )

    report = run_community_credential_surface_gate()

    assert report.ok, f"unexpected credential surface violations: {report.violations}"
    allowed = {(f.file, f.function, f.kind, f.symbol) for f in report.findings}
    assert (
        "okto_pulse/community/seed.py",
        "seed_community_defaults",
        "return_secret",
        "api_key",
    ) in allowed
    assert (
        "okto_pulse/community/cli.py",
        "_exportable_credential_from_legacy_agent",
        "persisted_agent_api_key_read",
        "api_key",
    ) in allowed
    assert all(f.allowlisted for f in report.findings)


def test_hnd2_credential_surface_gate_blocks_printing_agent_api_key(tmp_path):
    from okto_pulse.community.adapters.credential_surface_gate import (
        run_community_credential_surface_gate,
    )

    rogue = tmp_path / "okto_pulse" / "community" / "rogue.py"
    rogue.parent.mkdir(parents=True)
    rogue.write_text(
        "def leak(agent):\n"
        "    print(agent.api_key)\n",
        encoding="utf-8",
    )

    report = run_community_credential_surface_gate(source_root=tmp_path)

    assert not report.ok
    kinds = {(f.kind, f.symbol) for f in report.violations}
    assert ("stdout_secret", "api_key") in kinds
    assert ("persisted_agent_api_key_read", "api_key") in kinds


def test_hnd2_credential_surface_gate_blocks_marker_mcp_json_interpolation(tmp_path):
    from okto_pulse.community.adapters.credential_surface_gate import (
        run_community_credential_surface_gate,
    )

    rogue = tmp_path / "okto_pulse" / "community" / "rogue.py"
    rogue.parent.mkdir(parents=True)
    rogue.write_text(
        "import json\n"
        "from pathlib import Path\n"
        "def export_marker(marker):\n"
        "    cfg = {'mcpServers': {'local': {\n"
        "        'url': f'http://127.0.0.1:8101/mcp?api_key={marker}'\n"
        "    }}}\n"
        "    Path('.mcp.json').write_text(json.dumps(cfg))\n",
        encoding="utf-8",
    )

    report = run_community_credential_surface_gate(source_root=tmp_path)

    assert not report.ok
    assert any(
        f.kind == "credential_interpolation" and f.symbol == "marker"
        for f in report.violations
    )


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


def test_hnd2_init_reveals_returned_key_but_not_persisted_marker(
    tmp_path, monkeypatch, capsys
):
    import okto_pulse.community.cli as cli
    import okto_pulse.community.adapters.composition as composition
    import okto_pulse.community.adapters.relational_schema_lifecycle as lifecycle
    import okto_pulse.community.config as community_config
    import okto_pulse.community.main as community_main
    import okto_pulse.community.seed as community_seed
    import okto_pulse.core.infra.auth as auth
    import okto_pulse.core.infra.config as config
    import okto_pulse.core.infra.database as database
    import okto_pulse.core.infra.storage as storage
    import okto_pulse.core.kg.interfaces as kg_interfaces
    from okto_pulse.community.cli import cmd_init

    persisted_marker = "sha256:must-not-be-printed"
    revealed_key = "dash_revealed_once"

    class Settings:
        def __init__(self):
            self.data_dir = str(tmp_path / "pulse")
            self.upload_dir = str(tmp_path / "pulse" / "uploads")
            self.kg_base_dir = str(tmp_path / "pulse" / "kg")
            self.metrics_dir = str(tmp_path / "pulse" / "metrics")
            self.database_url = "sqlite+aiosqlite:///:memory:"
            self.mcp_port = 8101

    async def fake_init_db():
        return None

    async def fake_close_db():
        return None

    async def fake_seed(_db):
        board = SimpleNamespace(id="board-1", name="Board")
        agent = SimpleNamespace(name="Agent", api_key=persisted_marker)
        return board, agent, revealed_key

    class _GraphSchema:
        async def ensure_bootstrapped(self, _board_id):
            return None

        async def current_version(self, _board_id):
            return "v1"

    class _GraphPath:
        def board_graph_path(self, _board_id):
            return str(tmp_path / "graph")

    registry = SimpleNamespace(
        graph_schema_manager=_GraphSchema(),
        graph_path_resolver=_GraphPath(),
    )

    monkeypatch.setattr(community_config, "CommunitySettings", Settings)
    monkeypatch.setattr(community_main, "_ensure_data_dir", lambda settings: None)
    monkeypatch.setattr(config, "configure_settings", lambda _settings: None)
    monkeypatch.setattr(auth, "configure_auth", lambda _provider: None)
    monkeypatch.setattr(storage, "configure_storage", lambda _provider: None)
    monkeypatch.setattr(composition, "community_storage_provider", lambda _path: None)
    monkeypatch.setattr(
        cli, "_configure_community_relational_runtime", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(database, "init_db", fake_init_db)
    monkeypatch.setattr(database, "close_db", fake_close_db)
    monkeypatch.setattr(database, "get_session_factory", lambda: lambda: _FakeSession())
    monkeypatch.setattr(community_seed, "seed_community_defaults", fake_seed)
    monkeypatch.setattr(
        lifecycle, "register_community_relational_schema_lifecycle", lambda: None
    )
    monkeypatch.setattr(kg_interfaces, "get_kg_registry", lambda: registry)

    cmd_init(SimpleNamespace(mcp_port=8101, agents=None))

    captured = capsys.readouterr()
    assert revealed_key in captured.out
    assert persisted_marker not in captured.out
    assert persisted_marker not in captured.err
