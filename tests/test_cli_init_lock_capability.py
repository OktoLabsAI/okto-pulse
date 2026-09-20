"""F4: init retains admission and no longer accepts the reset-only bypass."""

from types import SimpleNamespace

import pytest

from okto_pulse.community import cli, config, serve_lock


def test_init_refuses_live_runtime_before_composition(tmp_path, monkeypatch, capsys):
    settings = SimpleNamespace(data_dir=str(tmp_path))
    monkeypatch.setattr(config, "CommunitySettings", lambda: settings)
    monkeypatch.setattr(
        cli,
        "_configure_community_relational_runtime",
        lambda *args, **kwargs: pytest.fail("live runtime reached composition"),
    )
    with serve_lock.ServeInstanceLock(tmp_path).acquire():
        with pytest.raises(SystemExit) as error:
            cli.cmd_init(SimpleNamespace())
    assert error.value.code == 2
    assert "ERROR [serve-lock]: refusing 'init'" in capsys.readouterr().err


def test_init_no_longer_accepts_reset_only_lock_capability(tmp_path):
    with serve_lock.ServeInstanceLock(tmp_path).acquire() as lock:
        with pytest.raises(TypeError, match="owned_serve_lock"):
            cli.cmd_init(SimpleNamespace(), owned_serve_lock=lock)
