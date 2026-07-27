"""Release bootstrap credential handoff regression tests."""

from __future__ import annotations

import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from okto_pulse.community import cli


BOOTSTRAP_CREDENTIAL = f"dash_{'ab' * 24}"


def test_release_handoff_is_private_and_consumed_exactly_once(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    handoff = tmp_path / "bootstrap-api-key"

    written = cli._write_bootstrap_key_handoff(handoff, BOOTSTRAP_CREDENTIAL)

    assert written == handoff
    assert handoff.read_text(encoding="ascii") == f"{BOOTSTRAP_CREDENTIAL}\n"
    if os.name != "nt":
        assert stat.S_IMODE(handoff.stat().st_mode) == 0o600

    cli.cmd_api_key(SimpleNamespace(handoff_file=str(handoff)))

    first = capsys.readouterr()
    assert first.out == f"{BOOTSTRAP_CREDENTIAL}\n"
    assert first.err == ""
    assert not handoff.exists()
    assert not list(tmp_path.glob(".*.claimed-*"))
    assert not list(tmp_path.glob(".*.pending-*"))

    with pytest.raises(SystemExit) as exc_info:
        cli.cmd_api_key(SimpleNamespace(handoff_file=str(handoff)))

    second = capsys.readouterr()
    assert exc_info.value.code == 1
    assert second.out == ""
    assert "missing or was already consumed" in second.err


def test_release_handoff_refuses_overwrite_without_removing_existing_file(
    tmp_path: Path,
) -> None:
    handoff = tmp_path / "bootstrap-api-key"
    handoff.write_text("operator-owned", encoding="utf-8")

    with pytest.raises(FileExistsError):
        cli._write_bootstrap_key_handoff(handoff, BOOTSTRAP_CREDENTIAL)

    assert handoff.read_text(encoding="utf-8") == "operator-owned"


def test_publication_race_preserves_destination_and_complete_pending_secret(
    tmp_path: Path,
) -> None:
    handoff = tmp_path / "bootstrap-api-key"
    reservation = cli._reserve_bootstrap_key_handoff(handoff)
    handoff.write_text("racing-operator", encoding="utf-8")

    with pytest.raises(RuntimeError, match="complete pending handoff retained"):
        reservation.publish(BOOTSTRAP_CREDENTIAL)

    assert handoff.read_text(encoding="utf-8") == "racing-operator"
    assert reservation.pending_path.read_text(encoding="ascii") == (
        f"{BOOTSTRAP_CREDENTIAL}\n"
    )
    assert (
        cli._consume_bootstrap_key_handoff(reservation.pending_path)
        == BOOTSTRAP_CREDENTIAL
    )
    reservation.discard()


def test_consumer_cannot_see_or_steal_unpublished_pending_handoff(
    tmp_path: Path,
) -> None:
    handoff = tmp_path / "bootstrap-api-key"
    reservation = cli._reserve_bootstrap_key_handoff(handoff)
    try:
        assert not handoff.exists()
        assert reservation.pending_path.exists()
        assert reservation.pending_path.stat().st_size == 0

        with pytest.raises(RuntimeError, match="missing or was already consumed"):
            cli._consume_bootstrap_key_handoff(handoff)

        assert reservation.pending_path.exists()
        reservation.publish(BOOTSTRAP_CREDENTIAL)
        assert cli._consume_bootstrap_key_handoff(handoff) == BOOTSTRAP_CREDENTIAL
    finally:
        reservation.discard()

    assert not handoff.exists()
    assert not list(tmp_path.glob(".*.pending-*"))


def test_concurrent_consumers_deliver_credential_to_exactly_one(
    tmp_path: Path,
) -> None:
    handoff = tmp_path / "bootstrap-api-key"
    cli._write_bootstrap_key_handoff(handoff, BOOTSTRAP_CREDENTIAL)
    start = threading.Barrier(2)

    def consume() -> tuple[str, str]:
        start.wait(timeout=5)
        try:
            return ("credential", cli._consume_bootstrap_key_handoff(handoff))
        except RuntimeError as exc:
            return ("error", str(exc))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: consume(), range(2)))

    credentials = [value for kind, value in results if kind == "credential"]
    errors = [value for kind, value in results if kind == "error"]
    assert credentials == [BOOTSTRAP_CREDENTIAL]
    assert len(errors) == 1
    assert "missing or was already consumed" in errors[0]
    assert not handoff.exists()


def test_release_handoff_consumes_and_rejects_invalid_payload(
    tmp_path: Path,
) -> None:
    handoff = tmp_path / "bootstrap-api-key"
    handoff.write_text("sha256:not-a-secret\n", encoding="ascii")
    if os.name != "nt":
        handoff.chmod(0o600)

    with pytest.raises(RuntimeError, match="handoff is invalid"):
        cli._consume_bootstrap_key_handoff(handoff)

    assert not handoff.exists()
    assert not list(tmp_path.glob(".*.claimed-*"))


def test_release_handoff_requires_absolute_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be absolute"):
        cli._write_bootstrap_key_handoff(
            Path("relative") / "bootstrap-api-key",
            BOOTSTRAP_CREDENTIAL,
        )


def test_init_preflight_rejects_existing_handoff_before_database_init(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import okto_pulse.core as core
    import okto_pulse.community.adapters.composition as composition
    import okto_pulse.community.adapters.relational_schema_lifecycle as lifecycle
    import okto_pulse.community.adapters.sqlalchemy_database as database
    import okto_pulse.community.auth as community_auth
    import okto_pulse.community.config as community_config
    import okto_pulse.community.main as community_main
    import okto_pulse.community.seed as community_seed

    handoff = tmp_path / "bootstrap-api-key"
    handoff.write_text("operator-owned", encoding="utf-8")
    calls: list[str] = []

    class Settings:
        data_dir = str(tmp_path / "pulse")
        upload_dir = str(tmp_path / "pulse" / "uploads")
        database_url = "sqlite+aiosqlite:///:memory:"
        mcp_port = 8101

    async def forbidden_init_db() -> None:
        calls.append("init_db")

    async def forbidden_seed(_db) -> None:
        calls.append("seed")

    monkeypatch.setattr(cli, "_fail_fast_if_server_running", lambda _op: None)
    monkeypatch.setattr(community_config, "CommunitySettings", Settings)
    monkeypatch.setattr(community_main, "_ensure_data_dir", lambda _settings: None)
    monkeypatch.setattr(core, "configure_settings", lambda _settings: None)
    monkeypatch.setattr(core, "configure_auth", lambda _provider: None)
    monkeypatch.setattr(core, "configure_storage", lambda _provider: None)
    monkeypatch.setattr(community_auth, "LocalAuthProvider", lambda: object())
    monkeypatch.setattr(composition, "community_storage_provider", lambda _path: None)
    monkeypatch.setattr(
        lifecycle, "register_community_relational_schema_lifecycle", lambda: None
    )
    monkeypatch.setattr(
        cli, "_configure_community_relational_runtime", lambda *_a, **_kw: None
    )
    monkeypatch.setattr(database, "init_db", forbidden_init_db)
    monkeypatch.setattr(community_seed, "seed_community_defaults", forbidden_seed)

    with pytest.raises(SystemExit) as exc_info:
        cli.cmd_init(
            SimpleNamespace(
                mcp_port=8101,
                agents=None,
                bootstrap_key_handoff=str(handoff),
            )
        )

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert calls == []
    assert "Unable to reserve bootstrap credential handoff" in captured.err
    assert handoff.read_text(encoding="utf-8") == "operator-owned"


def test_release_workflow_uses_tmpfs_handoff_instead_of_database_recovery() -> None:
    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"
    ).read_text(encoding="utf-8")

    assert (
        "--tmpfs /run/okto-pulse-release:rw,noexec,nosuid,nodev,mode=0700" in workflow
    )
    assert "-e HOST=0.0.0.0" in workflow
    assert "-e MCP_HOST=0.0.0.0" in workflow
    assert "-e DATA_DIR=/data" in workflow
    assert "-e KG_BASE_DIR=/data" in workflow
    assert "-p 127.0.0.1:8100:8100" in workflow
    assert "-p 127.0.0.1:8101:8101" in workflow
    assert "-p 8100:8100" not in workflow
    assert "-p 8101:8101" not in workflow
    assert (
        "--bootstrap-key-handoff /run/okto-pulse-release/bootstrap-api-key" in workflow
    )
    assert "--handoff-file /run/okto-pulse-release/bootstrap-api-key" in workflow
    assert "bootstrap API key handoff was readable more than once" in workflow
    assert "Read the bootstrap key directly from the seeded DB" not in workflow
    assert '|| echo "Replay tool exited non-zero' not in workflow
