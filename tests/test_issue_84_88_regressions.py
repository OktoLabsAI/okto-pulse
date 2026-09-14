"""GitHub #84, #85, #87, #88: isolated CLI and configuration regressions."""

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from okto_pulse.community import acceptance, cli
from okto_pulse.community.config import CommunitySettings


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data-home"))
    monkeypatch.delenv("OKTO_PULSE_HOME", raising=False)
    monkeypatch.delenv("OKTO_PULSE_TERMS_ACCEPTED", raising=False)
    monkeypatch.delenv("CORS_ORIGINS", raising=False)


@pytest.mark.parametrize("group", [["kg"], ["kg", "subtype"], ["metrics"], ["code-traceability"]])
def test_missing_child_shows_help(monkeypatch, capsys, group):
    monkeypatch.setattr(sys, "argv", ["okto-pulse", *group])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    captured = capsys.readouterr()
    assert "usage:" in captured.out
    assert "Traceback" not in captured.out + captured.err
    if group == ["kg", "subtype"]:
        assert "declare" in captured.out


@pytest.mark.parametrize("source", ["env", "dotenv", "constructor", "default", "empty"])
def test_cors_configuration_is_honored(source, tmp_path, monkeypatch):
    origins = "https://example.com, https://other.example"
    kwargs = {}
    if source == "env":
        monkeypatch.setenv("CORS_ORIGINS", origins)
    elif source == "dotenv":
        (tmp_path / ".env").write_text(f"CORS_ORIGINS={origins}\n", encoding="utf-8")
    elif source == "constructor":
        monkeypatch.setenv("CORS_ORIGINS", "https://ignored.example")
        kwargs["cors_origins"] = origins
    elif source == "empty":
        kwargs["cors_origins"] = ""
    settings = CommunitySettings(**kwargs)
    expected = ["*"] if source == "default" else ([] if source == "empty" else origins.split(", "))
    assert settings.cors_origins_list == expected


def test_cors_middleware_accepts_only_configured_origin(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", "https://allowed.example")
    settings = CommunitySettings()
    app = Starlette(routes=[Route("/", lambda request: JSONResponse({"ok": True}))])
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins_list, allow_methods=["*"])
    with TestClient(app) as client:
        for origin, status in [("https://allowed.example", 200), ("https://denied.example", 400)]:
            response = client.options("/", headers={"Origin": origin, "Access-Control-Request-Method": "GET"})
            assert response.status_code == status
            assert response.headers.get("access-control-allow-origin") == (origin if status == 200 else None)


@pytest.mark.parametrize("source", ["env", "legacy", "both", "dotenv", "default", "blank"])
def test_acceptance_uses_settings_data_home(source, tmp_path, monkeypatch):
    monkeypatch.delenv("DATA_DIR")
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "user-home")
    if source in {"env", "both"}:
        monkeypatch.setenv("DATA_DIR", str(tmp_path / "primary"))
    if source in {"legacy", "both", "blank"}:
        monkeypatch.setenv("OKTO_PULSE_HOME", str(tmp_path / "legacy"))
    if source == "blank":
        monkeypatch.setenv("DATA_DIR", "  ")
    if source == "dotenv":
        (tmp_path / ".env").write_text("DATA_DIR=./dotenv-home\n", encoding="utf-8")
    expected = Path(CommunitySettings().data_dir) / ".terms-accepted.json"
    acceptance.write_acceptance("cli")
    assert acceptance._state_path() == expected
    assert json.loads(expected.read_text(encoding="utf-8"))["source"] == "cli"
    assert acceptance.acceptance_status()["pre_accepted"] is True
    if source == "both":
        assert not (tmp_path / "legacy/.terms-accepted.json").exists()


@pytest.fixture
def export_runtime(monkeypatch):
    from okto_pulse.community.adapters import composition, relational_schema_lifecycle, sqlalchemy_database
    from okto_pulse.core.application import kg_operations

    async def init_db():
        pass

    monkeypatch.setattr(cli, "_fail_fast_if_server_running", lambda _: None)
    monkeypatch.setattr(cli, "_configure_community_relational_runtime", lambda *a, **kw: None)
    monkeypatch.setattr(relational_schema_lifecycle, "register_community_relational_schema_lifecycle", lambda: None)
    monkeypatch.setattr(sqlalchemy_database, "init_db", init_db)
    monkeypatch.setattr(sqlalchemy_database, "get_session_factory", lambda: None)
    monkeypatch.setattr(composition, "configure_community_kg_registry", lambda _: None)
    exporter = Mock(return_value={"nodes_exported": 1, "edges_exported": 0, "title": "ação"})
    monkeypatch.setattr(kg_operations, "export_board_jsonld", exporter)
    return exporter


def export_to(path):
    cli.cmd_kg_export(SimpleNamespace(board_id="test-board", output=str(path), format="jsonld"))


def test_export_missing_parent_fails_before_graph_setup(export_runtime, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_configure_community_relational_runtime", lambda *a, **kw: pytest.fail("must preflight before setup"))
    with pytest.raises(SystemExit) as error:
        export_to(tmp_path / "missing/export.jsonld")
    assert error.value.code == 2
    assert "ERRO export_output_error:" in capsys.readouterr().out
    export_runtime.assert_not_called()
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize("phase", ["mkstemp", "fdopen", "write", "replace", "interrupt"])
def test_export_failure_preserves_destination_and_cleans_temp(export_runtime, tmp_path, monkeypatch, capsys, phase):
    destination = tmp_path / "export.jsonld"
    destination.write_text("original", encoding="utf-8")
    failure = KeyboardInterrupt() if phase == "interrupt" else PermissionError("test output failure")

    def refuse(*args, **kwargs):
        raise failure

    if phase == "mkstemp":
        monkeypatch.setattr(tempfile, "mkstemp", refuse)
    elif phase == "fdopen":
        monkeypatch.setattr(os, "fdopen", refuse)
    elif phase == "write":
        real_fdopen = os.fdopen

        class BrokenWriter:
            def __init__(self, *args, **kwargs):
                self.handle = real_fdopen(*args, **kwargs)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.handle.close()

            write = refuse

        monkeypatch.setattr(os, "fdopen", BrokenWriter)
    else:
        monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(KeyboardInterrupt if phase == "interrupt" else SystemExit) as error:
        export_to(destination)
    if phase != "interrupt":
        assert error.value.code == 2
        assert "ERRO export_output_error:" in capsys.readouterr().out
    assert destination.read_text(encoding="utf-8") == "original"
    assert list(tmp_path.glob("*.tmp")) == []


def test_export_success_replaces_complete_json(export_runtime, tmp_path, capsys):
    destination = tmp_path / "export.jsonld"
    destination.write_text("old", encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        export_to(destination)
    assert error.value.code == 0
    assert json.loads(destination.read_text(encoding="utf-8")) == export_runtime.return_value
    assert list(tmp_path.glob("*.tmp")) == []
    assert "Export concluido:" in capsys.readouterr().out
