"""GitHub #84, #85, #87, #88: isolated CLI and configuration regressions."""

import json
import sys
from pathlib import Path

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


@pytest.mark.parametrize("group", [["metrics"], ["code-traceability"]])
def test_missing_child_shows_help(monkeypatch, capsys, group):
    monkeypatch.setattr(sys, "argv", ["okto-pulse", *group])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    captured = capsys.readouterr()
    assert "usage:" in captured.out
    assert "Traceback" not in captured.out + captured.err


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
