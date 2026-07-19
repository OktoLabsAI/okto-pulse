"""A5R renewal resilience: bounded Windows PermissionError retry at the exact
writer-manifest atomic replace, plus the Core typed fence-loss boundary.

The v5 installed proof surfaced a real production defect: a transient Windows
sharing violation (PermissionError [WinError 5]) at the renewal ``os.replace``
escaped the Community port raw, crossed the Core lease boundary untyped and
let the worker's generic handler terminalize FAILED/native_operation_failed
after durable physical work.  These tests pin the corrective contract at both
layers: the port retries ONLY that replace within an explicit tested bound and
can never resurrect a stale/expired/foreign token, and the Core lease
translates any OS-level renewal fault into GlobalDiscoveryWriterFenceLost with
the original error chained as its cause.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

import okto_pulse.community.adapters.coordination as coordination_module
from okto_pulse.community.adapters.coordination import CommunityLocalWriteLockPort
from okto_pulse.core.kg.global_discovery_writer import (
    GlobalDiscoveryWriterFenceLost,
    GlobalDiscoveryWriterLease,
)
from okto_pulse.core.kg.single_writer_lock import KGSingleWriterLock

_BOARD_ID = "board-renewal"
_ARTIFACT_ID = "writer.lock"


def _acquire(port: CommunityLocalWriteLockPort, tmp_path: Path, *, ttl_seconds=30):
    acquisition = port.acquire_single_writer_sync(
        board_id=_BOARD_ID,
        artifact_id=_ARTIFACT_ID,
        operation="renewal-test",
        owner_id="renewal-owner",
        ttl_seconds=ttl_seconds,
        base_dir_hint=str(tmp_path),
    )
    assert acquisition.acquired and acquisition.owner_token
    return acquisition


def _renew(port: CommunityLocalWriteLockPort, tmp_path: Path, *, owner_token, ttl_seconds=45):
    return port.renew_single_writer_sync(
        board_id=_BOARD_ID,
        artifact_id=_ARTIFACT_ID,
        owner_token=owner_token,
        ttl_seconds=ttl_seconds,
        base_dir_hint=str(tmp_path),
    )


def _manifest(port: CommunityLocalWriteLockPort, tmp_path: Path):
    return port.inspect_single_writer_sync(
        board_id=_BOARD_ID,
        artifact_id=_ARTIFACT_ID,
        base_dir_hint=str(tmp_path),
    )


def _board_dir(port: CommunityLocalWriteLockPort, tmp_path: Path) -> Path:
    return port._single_writer_board_dir(  # noqa: SLF001
        _BOARD_ID,
        base_dir_hint=str(tmp_path),
        board_dir_resolver=None,
    )


def _assert_no_debris(board_dir: Path) -> None:
    from okto_pulse.core.kg.single_writer_lock import RECOVERY_LOCK_FILENAME

    leftovers = [
        child.name
        for child in board_dir.iterdir()
        if ".renewing" in child.name or child.name == RECOVERY_LOCK_FILENAME
    ]
    assert leftovers == []


def _install_denying_replace(
    monkeypatch: pytest.MonkeyPatch,
    *,
    deny_times: int,
    on_first_denial=None,
):
    """Deny only renewal-temporary replaces; everything else passes through."""

    denials: list[str] = []
    passthrough: list[str] = []
    real_replace = os.replace

    def wrapped(src, dst, *args, **kwargs):
        if ".renewing" in str(src):
            if len(denials) < deny_times:
                denials.append(str(src))
                if on_first_denial is not None and len(denials) == 1:
                    on_first_denial()
                raise PermissionError(5, "sharing violation (injected)")
            passthrough.append(str(src))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(coordination_module.os, "replace", wrapped)
    return denials, passthrough


def test_transient_replace_denial_retries_and_renews_the_exact_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = CommunityLocalWriteLockPort()
    acquisition = _acquire(port, tmp_path)
    before = _manifest(port, tmp_path)
    denials, passthrough = _install_denying_replace(monkeypatch, deny_times=1)

    renewed = _renew(port, tmp_path, owner_token=acquisition.owner_token)

    assert renewed is True
    assert len(denials) == 1
    assert len(passthrough) == 1
    after = _manifest(port, tmp_path)
    assert after is not None and before is not None
    assert after.owner_token == acquisition.owner_token == before.owner_token
    assert after.owner_id == before.owner_id
    assert after.acquired_at_epoch == before.acquired_at_epoch
    assert after.expires_at_epoch > before.expires_at_epoch
    _assert_no_debris(_board_dir(port, tmp_path))


def test_replace_denial_exhaustion_is_bounded_and_raises_the_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = CommunityLocalWriteLockPort()
    acquisition = _acquire(port, tmp_path)
    before = _manifest(port, tmp_path)
    denials, passthrough = _install_denying_replace(monkeypatch, deny_times=10**9)

    started = time.monotonic()
    with pytest.raises(PermissionError) as excinfo:
        _renew(port, tmp_path, owner_token=acquisition.owner_token)
    elapsed = time.monotonic() - started

    # The bound is explicit: exactly the declared attempt count, nothing more.
    assert (
        len(denials)
        == coordination_module._SINGLE_WRITER_RENEW_REPLACE_ATTEMPTS  # noqa: SLF001
        == 3
    )
    assert passthrough == []
    assert excinfo.value.args == (5, "sharing violation (injected)")
    assert elapsed < 2.0
    after = _manifest(port, tmp_path)
    assert after is not None and before is not None
    assert after.owner_token == before.owner_token
    assert after.expires_at_epoch == before.expires_at_epoch
    _assert_no_debris(_board_dir(port, tmp_path))


def test_expiry_between_denied_attempts_is_never_resurrected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    port = CommunityLocalWriteLockPort()
    acquisition = _acquire(port, tmp_path)
    manifest_path = port._single_writer_path(  # noqa: SLF001
        _board_dir(port, tmp_path), _ARTIFACT_ID
    )

    def expire_manifest() -> None:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        data["expires_at_epoch"] = time.time() - 1
        manifest_path.write_text(json.dumps(data), encoding="utf-8")

    denials, passthrough = _install_denying_replace(
        monkeypatch,
        deny_times=10**9,
        on_first_denial=expire_manifest,
    )

    renewed = _renew(port, tmp_path, owner_token=acquisition.owner_token)

    # The retry revalidates expiry against a fresh clock and refuses to
    # resurrect: exactly one denied replace, then False with no further tries.
    assert renewed is False
    assert len(denials) == 1
    assert passthrough == []
    _assert_no_debris(_board_dir(port, tmp_path))


def test_expiry_during_same_attempt_fsync_is_never_resurrected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1/AC3: the lease expires WHILE the renewal temp is being made
    durable (slow fsync).  The fresh-clock recheck immediately before
    publication must fail closed: False, zero effective replaces, manifest
    untouched, no debris — an expired lease is never resurrected even when
    identity equality with the attempt snapshot still holds."""

    port = CommunityLocalWriteLockPort()
    acquisition = _acquire(port, tmp_path, ttl_seconds=1)
    before = _manifest(port, tmp_path)

    real_fsync = os.fsync
    delayed: list[bool] = []

    def delaying_fsync(fd):
        if not delayed:
            delayed.append(True)
            time.sleep(1.5)
        return real_fsync(fd)

    monkeypatch.setattr(coordination_module.os, "fsync", delaying_fsync)

    replaces: list[str] = []
    real_replace = os.replace

    def counting_replace(src, dst, *args, **kwargs):
        if ".renewing" in str(src):
            replaces.append(str(src))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(coordination_module.os, "replace", counting_replace)

    renewed = _renew(port, tmp_path, owner_token=acquisition.owner_token)

    assert renewed is False
    assert delayed == [True]
    assert replaces == []
    after = _manifest(port, tmp_path)
    assert after is not None and before is not None
    assert after.owner_token == before.owner_token
    assert after.expires_at_epoch == before.expires_at_epoch
    _assert_no_debris(_board_dir(port, tmp_path))


def test_foreign_token_stays_false_with_zero_replace_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = CommunityLocalWriteLockPort()
    _acquire(port, tmp_path)
    denials, passthrough = _install_denying_replace(monkeypatch, deny_times=10**9)

    renewed = _renew(port, tmp_path, owner_token="not-the-owner")

    assert renewed is False
    assert denials == []
    assert passthrough == []
    _assert_no_debris(_board_dir(port, tmp_path))


def test_core_renew_types_os_faults_as_fence_lost_with_the_cause_chained() -> None:
    class _OsFaultLock:
        def renew(self, **_kwargs):
            raise PermissionError(5, "sharing violation (injected)")

    lease = GlobalDiscoveryWriterLease(
        lock=_OsFaultLock(),
        owner_token="token",
        operation="renewal-boundary",
        ttl_seconds=30,
    )
    with pytest.raises(GlobalDiscoveryWriterFenceLost) as excinfo:
        lease.renew()
    assert isinstance(excinfo.value.__cause__, PermissionError)
    assert excinfo.value.__cause__.args == (5, "sharing violation (injected)")


def test_core_renew_keeps_false_and_released_as_plain_fence_lost() -> None:
    class _RefusingLock:
        def renew(self, **_kwargs):
            return False

    lease = GlobalDiscoveryWriterLease(
        lock=_RefusingLock(),
        owner_token="token",
        operation="renewal-boundary",
        ttl_seconds=30,
    )
    with pytest.raises(GlobalDiscoveryWriterFenceLost) as excinfo:
        lease.renew()
    assert excinfo.value.__cause__ is None

    released = GlobalDiscoveryWriterLease(
        lock=_RefusingLock(),
        owner_token="token",
        operation="renewal-boundary",
        ttl_seconds=30,
        released=True,
    )
    with pytest.raises(GlobalDiscoveryWriterFenceLost):
        released.renew()


def test_core_renew_does_not_swallow_semantic_errors() -> None:
    class _SemanticLock:
        def renew(self, **_kwargs):
            raise ValueError("ttl_seconds outside the supported writer-lease range")

    lease = GlobalDiscoveryWriterLease(
        lock=_SemanticLock(),
        owner_token="token",
        operation="renewal-boundary",
        ttl_seconds=30,
    )
    with pytest.raises(ValueError):
        lease.renew()


def test_real_lock_chain_surfaces_typed_fence_lost_after_exhaustion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.core.ports.coordination import register_coordination_providers

    port = CommunityLocalWriteLockPort()
    register_coordination_providers(write_lock_port=port)
    lease = GlobalDiscoveryWriterLease.acquire(
        operation="renewal-chain-test",
        owner_id="a5r-chain",
        ttl_seconds=30,
        lock=KGSingleWriterLock(base_dir=tmp_path / "writer-locks"),
    )
    try:
        denials, _ = _install_denying_replace(monkeypatch, deny_times=10**9)
        with pytest.raises(GlobalDiscoveryWriterFenceLost) as excinfo:
            lease.renew()
        assert isinstance(excinfo.value.__cause__, PermissionError)
        assert (
            len(denials)
            == coordination_module._SINGLE_WRITER_RENEW_REPLACE_ATTEMPTS  # noqa: SLF001
        )
    finally:
        lease.release()
