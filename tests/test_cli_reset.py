"""Unit tests for the `okto-pulse reset` CLI command.

Tests argparse wiring via subprocess (matching the pattern in
test_cli_kg_backfill.py).  Full cmd_reset flow requires user input
and file system access so we only test the subparser shape here.
"""

from __future__ import annotations

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

# ---------------------------------------------------------------------------
# Argparse wiring
# ---------------------------------------------------------------------------


def test_reset_subparser_has_yes_flag():
    """The reset subparser exposes -y/--yes."""
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, r'{}'); "
         "from okto_pulse.community.cli import main; main()".format(str(REPO_SRC)),
         "reset", "--help"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0
    assert "-y" in result.stdout
    assert "--yes" in result.stdout


def test_reset_refuses_live_server_before_deleting_any_data(
    tmp_path,
    monkeypatch,
    capsys,
):
    import okto_pulse.community.config as community_config
    from okto_pulse.community import cli
    from okto_pulse.community import serve_lock

    data_dir = tmp_path / "pulse-data"
    database = data_dir / "data" / "pulse.db"
    upload = data_dir / "uploads" / "evidence.txt"
    database.parent.mkdir(parents=True)
    upload.parent.mkdir(parents=True)
    database.write_bytes(b"database-before-reset")
    upload.write_bytes(b"upload-before-reset")

    class Settings:
        def __init__(self):
            self.data_dir = str(data_dir)

    monkeypatch.setattr(community_config, "CommunitySettings", Settings)
    monkeypatch.setattr(
        cli,
        "cmd_init",
        lambda *_args, **_kwargs: pytest.fail("reset must not re-seed"),
    )

    lock = serve_lock.ServeInstanceLock(data_dir).acquire()
    try:
        with pytest.raises(SystemExit) as exc_info:
            cli.cmd_reset(SimpleNamespace(yes=True))
    finally:
        lock.release()

    assert exc_info.value.code == 2
    assert database.read_bytes() == b"database-before-reset"
    assert upload.read_bytes() == b"upload-before-reset"
    assert "refusing 'reset'" in capsys.readouterr().err


def test_reset_holds_serve_lock_through_reseed(
    tmp_path,
    monkeypatch,
):
    import okto_pulse.community.config as community_config
    from okto_pulse.community import cli
    from okto_pulse.community import serve_lock

    data_dir = tmp_path / "pulse-data"
    database = data_dir / "data" / "pulse.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"database-before-reset")

    class Settings:
        def __init__(self):
            self.data_dir = str(data_dir)

    monkeypatch.setattr(community_config, "CommunitySettings", Settings)
    reseeded = False

    def assert_fenced_init(_args, *, owned_serve_lock=None):
        nonlocal reseeded
        assert owned_serve_lock is serve_lock.get_active_lock()
        assert owned_serve_lock.is_acquired is True
        assert owned_serve_lock.data_dir == data_dir.resolve()
        with pytest.raises(serve_lock.ServeAlreadyRunningError):
            serve_lock.ServeInstanceLock(data_dir).acquire()
        reseeded = True

    monkeypatch.setattr(cli, "cmd_init", assert_fenced_init)

    cli.cmd_reset(SimpleNamespace(yes=True))

    assert reseeded is True
    assert not database.exists()
    assert not (data_dir / serve_lock.LOCK_FILENAME).exists()


@pytest.mark.parametrize("released", [False, True])
def test_init_rejects_unowned_or_released_lock_capability(
    tmp_path,
    monkeypatch,
    released,
):
    import okto_pulse.community.config as community_config
    from okto_pulse.community import cli
    from okto_pulse.community import serve_lock

    data_dir = tmp_path / "pulse-data"
    lock_dir = data_dir if released else tmp_path / "different-data"

    class Settings:
        def __init__(self):
            self.data_dir = str(data_dir)

    monkeypatch.setattr(community_config, "CommunitySettings", Settings)
    lock = serve_lock.ServeInstanceLock(lock_dir).acquire()
    if released:
        lock.release()

    try:
        with pytest.raises(RuntimeError, match="active serve-lock capability"):
            cli.cmd_init(SimpleNamespace(), owned_serve_lock=lock)
    finally:
        lock.release()
