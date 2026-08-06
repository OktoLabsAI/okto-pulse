from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from okto_pulse.community import cli
from okto_pulse.community.config import CommunitySettings
from okto_pulse.community.data_home import (
    UninitializedDefaultDataHomeError,
    assert_serve_data_home_ready,
    data_home_banner_lines,
)


def _clear_data_home_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.delenv("OKTO_PULSE_HOME", raising=False)
    monkeypatch.delenv("DATA_DIR_ORIGIN", raising=False)


def test_data_home_origin_precedence_is_resolved_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    explicit = tmp_path / "explicit"
    data_dir = tmp_path / "data-dir"
    legacy_home = tmp_path / "legacy-home"
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("OKTO_PULSE_HOME", str(legacy_home))

    explicit_settings = CommunitySettings(data_dir=str(explicit), _env_file=None)
    assert explicit_settings.data_dir == str(explicit.resolve())
    assert explicit_settings.data_dir_origin == "explicit"

    data_dir_settings = CommunitySettings(_env_file=None)
    assert data_dir_settings.data_dir == str(data_dir.resolve())
    assert data_dir_settings.data_dir_origin == "DATA_DIR"

    monkeypatch.delenv("DATA_DIR")
    legacy_settings = CommunitySettings(_env_file=None)
    assert legacy_settings.data_dir == str(legacy_home.resolve())
    assert legacy_settings.data_dir_origin == "OKTO_PULSE_HOME"

    monkeypatch.delenv("OKTO_PULSE_HOME")
    default_settings = CommunitySettings(_env_file=None)
    assert default_settings.data_dir_origin == "default"


def test_explicit_empty_data_dir_does_not_masquerade_as_data_dir_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_data_home_env(monkeypatch)
    monkeypatch.setenv("DATA_DIR", " \t ")
    monkeypatch.setenv("OKTO_PULSE_HOME", str(tmp_path / "legacy-home"))

    settings = CommunitySettings(data_dir="", _env_file=None)

    assert settings.data_dir == str((tmp_path / "legacy-home").resolve())
    assert settings.data_dir_origin == "OKTO_PULSE_HOME"


def test_explicit_empty_data_dir_falls_through_to_data_dir_before_legacy_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data-dir"
    legacy_home = tmp_path / "legacy-home"
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("OKTO_PULSE_HOME", str(legacy_home))

    settings = CommunitySettings(data_dir=" \t ", _env_file=None)

    assert settings.data_dir == str(data_dir.resolve())
    assert settings.data_dir_origin == "DATA_DIR"


def test_os_legacy_home_precedes_dotenv_data_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_data_home_env(monkeypatch)
    legacy_home = tmp_path / "legacy-env"
    dotenv_data = tmp_path / "dotenv-data"
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(f"DATA_DIR={dotenv_data}\n", encoding="utf-8")
    monkeypatch.setenv("OKTO_PULSE_HOME", str(legacy_home))

    settings = CommunitySettings(_env_file=dotenv_path)

    assert settings.data_dir == str(legacy_home.resolve())
    assert settings.data_dir_origin == "OKTO_PULSE_HOME"


def test_dotenv_data_dir_equal_to_default_keeps_source_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_data_home_env(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    default_home = tmp_path / ".okto-pulse"
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(f"DATA_DIR={default_home}\n", encoding="utf-8")

    settings = CommunitySettings(_env_file=dotenv_path)

    assert settings.data_dir == str(default_home.resolve())
    assert settings.data_dir_origin == "DATA_DIR"


def test_default_uninitialized_home_fails_before_creating_any_path(
    tmp_path: Path,
) -> None:
    data_home = tmp_path / "missing-default"
    settings = SimpleNamespace(
        data_dir=str(data_home),
        data_dir_origin="default",
    )

    with pytest.raises(UninitializedDefaultDataHomeError) as exc_info:
        assert_serve_data_home_ready(settings)

    assert str(exc_info.value) == "Run okto-pulse init or set DATA_DIR"
    assert not data_home.exists()


@pytest.mark.parametrize("origin", ["explicit", "DATA_DIR", "OKTO_PULSE_HOME"])
def test_non_default_origins_allow_a_new_home(tmp_path: Path, origin: str) -> None:
    data_home = tmp_path / origin
    settings = SimpleNamespace(data_dir=str(data_home), data_dir_origin=origin)

    assert assert_serve_data_home_ready(settings) == data_home.resolve()
    assert not data_home.exists()


@pytest.mark.parametrize("origin", ["explicit", "DATA_DIR"])
def test_new_explicit_home_acquires_lock_with_resolved_origin(
    tmp_path: Path,
    origin: str,
) -> None:
    from okto_pulse.community import serve_lock

    data_home = tmp_path / f"new-{origin}"
    settings = SimpleNamespace(data_dir=str(data_home), data_dir_origin=origin)

    lock = serve_lock.acquire_serve_lock(settings)
    try:
        payload = json.loads(
            (data_home / serve_lock.LOCK_FILENAME).read_text(encoding="utf-8")
        )
        assert data_home.is_dir()
        assert payload["data_dir"] == str(data_home.resolve())
        assert payload["data_dir_origin"] == origin
    finally:
        lock.release()


def test_initialized_default_home_is_allowed(tmp_path: Path) -> None:
    data_home = tmp_path / "initialized-default"
    database = data_home / "data" / "pulse.db"
    database.parent.mkdir(parents=True)
    database.touch()
    settings = SimpleNamespace(data_dir=str(data_home), data_dir_origin="default")

    assert assert_serve_data_home_ready(settings) == data_home.resolve()


def test_existing_default_serve_prints_warning_and_identity_before_urls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from okto_pulse.community import config as community_config
    from okto_pulse.community import main as community_main
    from okto_pulse.community import serve_lock

    data_home = tmp_path / "existing-default"
    database = data_home / "data" / "pulse.db"
    database.parent.mkdir(parents=True)
    database.touch()

    class Settings:
        data_dir = str(data_home)
        data_dir_origin = "default"

    monkeypatch.delenv("OKTO_PULSE_TERMS_ACCEPTED", raising=False)
    monkeypatch.setattr(community_config, "CommunitySettings", Settings)
    monkeypatch.setattr(cli, "_is_port_in_use", lambda _port: False)
    monkeypatch.setattr(
        serve_lock,
        "acquire_serve_lock",
        lambda _settings: nullcontext(),
    )
    monkeypatch.setattr(community_main, "run", lambda: 0)

    cli.cmd_serve(SimpleNamespace(api_port=38110, mcp_port=38111, accept_terms=False))

    output = capsys.readouterr().out
    assert output.count("Warning: Using the implicit default data home") == 1
    assert output.index("Data home:") < output.index("http://127.0.0.1:38110")
    assert output.index("Source: default") < output.index("http://127.0.0.1:38110")


def test_banner_always_names_resolved_home_and_source(tmp_path: Path) -> None:
    settings = SimpleNamespace(
        data_dir=str(tmp_path / "pulse-home"),
        data_dir_origin="default",
    )

    lines = data_home_banner_lines(settings)

    assert lines[0] == f"Data home: {(tmp_path / 'pulse-home').resolve()}"
    assert lines[1] == "Source: default"
    assert any("Warning:" in line for line in lines)


def test_cmd_serve_default_preflight_exits_two_without_lock_or_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from okto_pulse.community import config as community_config
    from okto_pulse.community import serve_lock

    data_home = tmp_path / "must-not-exist"

    class Settings:
        data_dir = str(data_home)
        data_dir_origin = "default"

    monkeypatch.setattr(community_config, "CommunitySettings", Settings)
    monkeypatch.setattr(
        cli,
        "_is_port_in_use",
        lambda _port: pytest.fail("port probes must run after data-home preflight"),
    )
    monkeypatch.setattr(
        serve_lock,
        "acquire_serve_lock",
        lambda _settings: pytest.fail("serve lock must not be acquired"),
    )

    before_ports = {
        "OKTO_PULSE_PORT": "before-api",
        "OKTO_PULSE_MCP_PORT": "before-mcp",
    }
    for key, value in before_ports.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(SystemExit) as exc:
        cli.cmd_serve(
            SimpleNamespace(api_port=38100, mcp_port=38101, accept_terms=False)
        )

    assert exc.value.code == 2
    assert capsys.readouterr().err == "Run okto-pulse init or set DATA_DIR\n"
    assert {key: __import__("os").environ[key] for key in before_ports} == before_ports
    assert not data_home.exists()


def test_main_run_default_preflight_returns_two_before_lock_or_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from okto_pulse.community import main as community_main
    from okto_pulse.community import serve_lock

    data_home = tmp_path / "direct-run-must-not-exist"

    class Settings:
        data_dir = str(data_home)
        data_dir_origin = "default"

    monkeypatch.setattr(community_main, "CommunitySettings", Settings)
    monkeypatch.setattr(
        community_main,
        "_enable_native_crash_diagnostics",
        lambda: pytest.fail("crash diagnostics must run after data-home preflight"),
    )
    monkeypatch.setattr(
        serve_lock,
        "acquire_serve_lock",
        lambda _settings: pytest.fail("serve lock must not be acquired"),
    )

    assert community_main.run() == 2
    assert capsys.readouterr().err == "Run okto-pulse init or set DATA_DIR\n"
    assert not data_home.exists()


def test_status_does_not_confirm_a_port_only_listener(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from okto_pulse.community import config as community_config

    data_home = tmp_path / "status-home"

    class Settings:
        data_dir = str(data_home)
        data_dir_origin = "explicit"

    monkeypatch.setattr(community_config, "CommunitySettings", Settings)
    monkeypatch.setattr(cli, "_is_port_in_use", lambda _port: True)

    cli.cmd_status(SimpleNamespace(api_port=38100, mcp_port=38101))

    output = capsys.readouterr().out
    assert "Runtime identity: unknown" in output
    assert "identity confirmed" not in output.lower()


def test_status_runtime_identity_matrix_confirms_only_live_fresh_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from okto_pulse.community import config as community_config
    from okto_pulse.community import serve_lock

    args = SimpleNamespace(api_port=38120, mcp_port=38121)

    def run_status(settings: SimpleNamespace, *, ports_up: bool) -> str:
        monkeypatch.setattr(community_config, "CommunitySettings", lambda: settings)
        monkeypatch.setattr(cli, "_is_port_in_use", lambda _port: ports_up)
        cli.cmd_status(args)
        return capsys.readouterr().out

    live = SimpleNamespace(
        data_dir=str(tmp_path / "live"),
        data_dir_origin="explicit",
    )
    lock = serve_lock.acquire_serve_lock(live)
    try:
        confirmed = run_status(live, ports_up=True)
    finally:
        lock.release()

    stale = SimpleNamespace(
        data_dir=str(tmp_path / "stale"),
        data_dir_origin="explicit",
    )
    stale_path = Path(stale.data_dir)
    stale_path.mkdir()
    (stale_path / serve_lock.LOCK_FILENAME).write_text(
        json.dumps(
            {
                "schema_version": 2,
                "instance_id": "stale-instance",
                "pid": 424242,
                "data_dir": str(stale_path.resolve()),
                "data_dir_origin": "explicit",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "heartbeat_at": (
                    datetime.now(timezone.utc)
                    - timedelta(seconds=serve_lock.HEARTBEAT_TTL_SECONDS + 5)
                ).isoformat(),
                "heartbeat_ttl_seconds": serve_lock.HEARTBEAT_TTL_SECONDS,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(serve_lock, "_pid_is_running", lambda _pid: False)
    stopped = run_status(stale, ports_up=False)

    missing_created = SimpleNamespace(
        data_dir=str(tmp_path / "missing-created"),
        data_dir_origin="explicit",
    )
    missing_created_path = Path(missing_created.data_dir)
    missing_created_path.mkdir()
    (missing_created_path / serve_lock.LOCK_FILENAME).write_text(
        json.dumps(
            {
                "schema_version": 2,
                "instance_id": "missing-created-instance",
                "pid": 424242,
                "data_dir": str(missing_created_path.resolve()),
                "data_dir_origin": "explicit",
                "heartbeat_at": datetime.now(timezone.utc).isoformat(),
                "heartbeat_ttl_seconds": serve_lock.HEARTBEAT_TTL_SECONDS,
            }
        ),
        encoding="utf-8",
    )
    identity_mismatch = run_status(missing_created, ports_up=True)

    corrupt = SimpleNamespace(
        data_dir=str(tmp_path / "corrupt"),
        data_dir_origin="explicit",
    )
    corrupt_path = Path(corrupt.data_dir)
    corrupt_path.mkdir()
    (corrupt_path / serve_lock.LOCK_FILENAME).write_text('{"pid":', encoding="utf-8")
    monkeypatch.setattr(serve_lock.time, "sleep", lambda _seconds: None)
    unreadable = run_status(corrupt, ports_up=False)

    port_only = SimpleNamespace(
        data_dir=str(tmp_path / "port-only"),
        data_dir_origin="explicit",
    )
    unknown = run_status(port_only, ports_up=True)

    outputs = (confirmed, stopped, identity_mismatch, unreadable, unknown)
    assert sum("Runtime identity: confirmed" in output for output in outputs) == 1
    assert "Runtime identity: stopped" in stopped
    assert "Runtime identity: identity mismatch" in identity_mismatch
    assert "Runtime identity: unreadable" in unreadable
    assert "Runtime identity: unknown" in unknown
