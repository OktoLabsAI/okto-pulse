"""Regression coverage for installation-local Community secrets."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import pytest

import okto_pulse.community.local_secrets as local_secrets
from okto_pulse.community.config import CommunitySettings
from okto_pulse.community.local_secrets import (
    CommunityLocalSecretError,
    GUIDELINE_POLICY_CURSOR_SECRET_RELATIVE_PATH,
    provision_guideline_policy_cursor_signing_key,
)
from okto_pulse.core.domain.guideline_policy import GuidelineRevisionPageCursor
from okto_pulse.core.inbound.guideline_policy_cursor import (
    policy_cursor_codec_from_settings,
)


def _settings(data_dir, *, signing_key=None) -> CommunitySettings:
    return CommunitySettings(
        data_dir=str(data_dir),
        guideline_policy_cursor_signing_key=signing_key,
        _env_file=None,
    )


def _secret_value(settings: CommunitySettings) -> str:
    secret = settings.guideline_policy_cursor_signing_key
    assert secret is not None
    return secret.get_secret_value()


def test_first_boot_persists_key_and_restart_reuses_it(tmp_path) -> None:
    first = _settings(tmp_path)
    path = provision_guideline_policy_cursor_signing_key(first)

    assert path == tmp_path / GUIDELINE_POLICY_CURSOR_SECRET_RELATIVE_PATH
    assert path.is_file()
    assert len(_secret_value(first).encode("ascii")) >= 32

    second = _settings(tmp_path)
    assert provision_guideline_policy_cursor_signing_key(second) == path
    assert _secret_value(second) == _secret_value(first)

    cursor = GuidelineRevisionPageCursor(
        revision_number=8,
        item_id="revision-local-secret",
        filter_digest="a" * 64,
        projection_digest="b" * 64,
    )
    token = policy_cursor_codec_from_settings(first).encode(cursor)
    assert (
        policy_cursor_codec_from_settings(second).decode(
            token,
            expected_kind="revision",
        )
        == cursor
    )


def test_environment_key_wins_without_writing_local_copy(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    explicit = "operator-managed-policy-cursor-secret-0001"
    monkeypatch.setenv("GUIDELINE_POLICY_CURSOR_SIGNING_KEY", explicit)
    settings = CommunitySettings(data_dir=str(tmp_path), _env_file=None)

    assert provision_guideline_policy_cursor_signing_key(settings) is None
    assert _secret_value(settings) == explicit
    assert not (tmp_path / GUIDELINE_POLICY_CURSOR_SECRET_RELATIVE_PATH).exists()


def test_different_installation_homes_receive_different_keys(tmp_path) -> None:
    first = _settings(tmp_path / "first")
    second = _settings(tmp_path / "second")

    provision_guideline_policy_cursor_signing_key(first)
    provision_guideline_policy_cursor_signing_key(second)

    assert _secret_value(first) != _secret_value(second)


def test_concurrent_first_boot_keeps_one_authoritative_key(tmp_path) -> None:
    def provision() -> str:
        settings = _settings(tmp_path)
        provision_guideline_policy_cursor_signing_key(settings)
        return _secret_value(settings)

    with ThreadPoolExecutor(max_workers=8) as executor:
        values = list(executor.map(lambda _index: provision(), range(16)))

    assert len(set(values)) == 1
    assert not list(
        (tmp_path / "secrets").glob(".guideline-policy-cursor.key.pending-*")
    )


def test_invalid_persisted_key_fails_closed_without_rotation(tmp_path) -> None:
    path = tmp_path / GUIDELINE_POLICY_CURSOR_SECRET_RELATIVE_PATH
    path.parent.mkdir(parents=True)
    path.write_text("too-short\n", encoding="ascii")
    original = path.read_bytes()
    settings = _settings(tmp_path)

    with pytest.raises(CommunityLocalSecretError, match="secret is invalid"):
        provision_guideline_policy_cursor_signing_key(settings)

    assert settings.guideline_policy_cursor_signing_key is None
    assert path.read_bytes() == original


def test_short_file_reads_are_completed(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_read = local_secrets.os.read

    def one_byte_at_a_time(descriptor: int, maximum_bytes: int) -> bytes:
        return original_read(descriptor, min(1, maximum_bytes))

    monkeypatch.setattr(local_secrets.os, "read", one_byte_at_a_time)
    settings = _settings(tmp_path)

    provision_guideline_policy_cursor_signing_key(settings)

    assert len(_secret_value(settings).encode("ascii")) == 64


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits only")
def test_existing_key_permissions_are_restricted(tmp_path) -> None:
    path = tmp_path / GUIDELINE_POLICY_CURSOR_SECRET_RELATIVE_PATH
    path.parent.mkdir(parents=True)
    path.write_text("x" * 64, encoding="ascii")
    path.chmod(0o644)
    settings = _settings(tmp_path)

    provision_guideline_policy_cursor_signing_key(settings)

    assert path.stat().st_mode & 0o077 == 0
