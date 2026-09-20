"""F4: retired commands fail before composition, including installed launchers."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import sysconfig

import pytest

from okto_pulse.community import cli, config


RETIRED_ARGUMENTS = (
    ("reset",),
    ("reset", "--yes"),
    ("reset", "--help"),
    ("verify-pipeline", "board-fixture"),
    ("verify-pipeline", "board-fixture", "--json"),
    ("verify-pipeline", "--help"),
)


@pytest.mark.parametrize("arguments", RETIRED_ARGUMENTS)
def test_retired_command_rejected_before_settings_or_dispatch(
    arguments, monkeypatch, capsys
):
    def forbidden(*args, **kwargs):
        pytest.fail("retired command reached composition or dispatch")

    monkeypatch.setattr(config, "CommunitySettings", forbidden)
    for name in vars(cli):
        if name.startswith("cmd_"):
            monkeypatch.setattr(cli, name, forbidden)
    monkeypatch.setattr(sys, "argv", ["okto-pulse", *arguments])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    output = capsys.readouterr()
    assert not output.out
    assert "invalid choice" in output.err
    assert arguments[0] in output.err


def test_retired_handlers_and_exclusive_reset_module_are_absent():
    assert not hasattr(cli, "cmd_reset")
    assert not hasattr(cli, "cmd_verify_pipeline")
    assert importlib.util.find_spec(
        "okto_pulse.community.commands.reset_graphs"
    ) is None


def test_help_retains_product_setup_and_observation(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["okto-pulse", "--help"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 0
    help_text = capsys.readouterr().out
    assert "reset" not in help_text
    assert "verify-pipeline" not in help_text
    for command in ("init", "serve", "status", "code-traceability", "metrics", "api-key"):
        assert command in help_text


def _tree(root):
    return {
        path.relative_to(root).as_posix(): (
            path.is_dir(),
            None if path.is_dir() else path.read_bytes(),
            path.stat().st_mtime_ns,
        )
        for path in root.rglob("*")
    }


@pytest.mark.parametrize("arguments", RETIRED_ARGUMENTS)
@pytest.mark.parametrize("existing", [False, True])
def test_installed_launcher_rejects_without_touching_data(tmp_path, arguments, existing):
    # No checkout path is injected into this child: exercise the wheel's console
    # script after the paired source/wheel/install provenance preflight.
    launcher = Path(sysconfig.get_path("scripts")) / (
        "okto-pulse.exe" if os.name == "nt" else "okto-pulse"
    )
    assert launcher.is_file(), "install the paired wheels before this qualification"
    data = tmp_path / "data-home"
    graph = tmp_path / "graph-home"
    if existing:
        for path, content in (
            (data / "data/pulse.db", b"opaque SQL fixture"),
            (data / "uploads/board-fixture/evidence", b"original evidence"),
            (graph / "boards/board-fixture/graph_backend_binding.json", b"original binding"),
            (graph / "boards/board-fixture/graphs/grafx/g1/sentinel", b"original graph"),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
    before = _tree(tmp_path)
    env = os.environ.copy()
    for key in ("PYTHONPATH", "DATABASE_URL"):
        env.pop(key, None)
    env.update(DATA_DIR=str(data), KG_BASE_DIR=str(graph))
    result = subprocess.run(
        [str(launcher), *arguments],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "invalid choice" in result.stderr
    assert not result.stdout
    assert _tree(tmp_path) == before


def test_current_guides_do_not_offer_retired_commands():
    root = Path(__file__).resolve().parents[1]
    for relative in ("README.md", "CLAUDE.md", "docs/kg-health.md"):
        text = (root / relative).read_text(encoding="utf-8")
        for command in ("okto-pulse reset", "okto-pulse verify-pipeline"):
            assert command not in text, (relative, command)
