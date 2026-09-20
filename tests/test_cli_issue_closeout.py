"""Issues #78-82: private SQLite/graph fixtures, never the user's data home."""

import json
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from okto_pulse.community import cli, config
from okto_pulse.community.commands import status


@pytest.fixture
def local_settings(tmp_path, monkeypatch):
    settings = SimpleNamespace(
        data_dir=str(tmp_path), data_dir_origin="test", kg_base_dir=str(tmp_path)
    )
    monkeypatch.setattr(config, "CommunitySettings", lambda: settings)
    monkeypatch.setattr(cli, "_is_port_in_use", lambda _: False)
    monkeypatch.setattr(
        status,
        "inspect_serve_lock_identity",
        lambda _: {"state": "stopped", "reason": "test"},
    )
    return settings


def database(settings, *, tables=True):
    path = Path(settings.data_dir) / "data/pulse.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn:
        if tables:
            for table in ("boards", "cards", "specs", "agents"):
                conn.execute(f"CREATE TABLE {table} (id TEXT PRIMARY KEY)")
                conn.execute(f"INSERT INTO {table} VALUES (?)", ("owned-board",))
            conn.commit()
    return path


@pytest.mark.parametrize("mode", ["ready", "uninitialized", "missing"])
def test_status_json_is_one_object_and_does_not_create_sqlite(
    local_settings, monkeypatch, capsys, mode
):
    if mode != "missing":
        database(local_settings, tables=mode == "ready")
    monkeypatch.setattr(sys, "argv", ["okto-pulse", "status", "--json"])
    cli.main()
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert output.err == ""
    assert result["database_status"] == mode
    assert result["boards"] == (1 if mode == "ready" else None)
    assert result["api_up"] is result["mcp_up"] is False
    assert result["error"] is None
    assert (Path(local_settings.data_dir) / "data/pulse.db").exists() == (
        mode != "missing"
    )


def test_status_text_retains_human_fields(local_settings, capsys):
    database(local_settings)
    cli.cmd_status(SimpleNamespace(api_port=8100, mcp_port=8101))
    output = capsys.readouterr().out
    assert "Okto Pulse Community Status" in output
    assert "  Boards:   1" in output
    assert "  Cards:    1" in output
    assert "  Runtime identity: stopped (test)" in output


@pytest.mark.parametrize("json_output", [False, True])
def test_invalid_sqlite_is_an_error_not_an_init_hint(
    local_settings, capsys, json_output
):
    path = database(local_settings, tables=False)
    path.write_bytes(b"not a SQLite database")
    with pytest.raises(SystemExit) as failure:
        cli.cmd_status(SimpleNamespace(api_port=0, mcp_port=0, json=json_output))
    output = capsys.readouterr()
    assert failure.value.code == 1
    assert "run 'okto-pulse init'" not in output.out + output.err
    assert "file is not a database" in output.out + output.err
    if json_output:
        assert json.loads(output.out)["error"]["type"] == "DatabaseError"
    else:
        assert "ERROR [status]" in output.err


@pytest.mark.parametrize(
    "failure",
    [PermissionError("access denied"), sqlite3.OperationalError("database is locked")],
)
def test_status_surfaces_unreadable_and_locked_errors(
    local_settings, monkeypatch, capsys, failure
):
    database(local_settings)

    def refuse(*args, **kwargs):
        raise failure

    monkeypatch.setattr(status.sqlite3, "connect", refuse)
    with pytest.raises(SystemExit) as error:
        cli.cmd_status(SimpleNamespace(api_port=0, mcp_port=0))
    assert error.value.code == 1
    assert str(failure) in capsys.readouterr().err


@pytest.mark.parametrize("value", ["0", "401", "-30", "999999999", "1.5", "invalid"])
def test_metrics_parser_refuses_bad_windows_before_dispatch(monkeypatch, capsys, value):
    monkeypatch.setattr(
        sys, "argv", ["okto-pulse", "metrics", "status", f"--window-days={value}"]
    )
    monkeypatch.setattr(
        cli, "cmd_metrics", lambda _: pytest.fail("invalid request dispatched")
    )
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert "--window-days" in capsys.readouterr().err


@pytest.mark.parametrize("value", [1, 30, 400])
def test_metrics_parser_preserves_valid_windows(monkeypatch, value):
    seen = []
    monkeypatch.setattr(
        sys, "argv", ["okto-pulse", "metrics", "status", f"--window-days={value}"]
    )
    monkeypatch.setattr(cli, "cmd_metrics", lambda args: seen.append(args.window_days))
    cli.main()
    assert seen == [value]


def test_direct_metrics_entry_also_validates_before_composition(capsys):
    with pytest.raises(SystemExit) as error:
        cli.cmd_metrics(SimpleNamespace(metrics_command="status", window_days=0))
    assert error.value.code == 2
    assert "between 1 and 400" in capsys.readouterr().err


def test_rest_and_cli_share_window_bounds():
    from okto_pulse.community.api import metrics
    from okto_pulse.community import metrics_limits

    assert metrics.MIN_WINDOW_DAYS == metrics_limits.MIN_WINDOW_DAYS == 1
    assert metrics.MAX_WINDOW_DAYS == metrics_limits.MAX_WINDOW_DAYS == 400
    assert metrics.DEFAULT_WINDOW_DAYS == cli.DEFAULT_WINDOW_DAYS == 30
