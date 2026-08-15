from __future__ import annotations

import asyncio
from pathlib import Path
import threading

import pytest

from okto_pulse.community.adapters.coordination import (
    CommunityLocalLeaseProvider,
    CommunityLocalWriteLockPort,
    build_root_bound_community_write_lock_port,
)


@pytest.mark.asyncio
async def test_af15_community_lease_provider_is_single_holder() -> None:
    provider = CommunityLocalLeaseProvider()

    first = await provider.try_acquire("kg_daily_tick", ttl_seconds=30)
    assert first is not None
    assert provider.is_held("kg_daily_tick")
    assert await provider.try_acquire("kg_daily_tick", ttl_seconds=30) is None

    await provider.release(first)
    assert not provider.is_held("kg_daily_tick")


@pytest.mark.asyncio
async def test_af15_community_write_lock_serializes_same_artifact() -> None:
    port = CommunityLocalWriteLockPort()
    entered: list[int] = []

    async def worker(index: int) -> None:
        handle = await port.acquire("board", "artifact")
        try:
            entered.append(index)
            await asyncio.sleep(0.01)
        finally:
            await port.release(handle)

    await asyncio.gather(worker(1), worker(2), worker(3))
    assert sorted(entered) == [1, 2, 3]
    assert not port.is_locked("board", "artifact")


def test_af15_root_bound_write_lock_renews_without_runtime_context(
    tmp_path: Path,
) -> None:
    from okto_pulse.core.kg.single_writer_lock import KGSingleWriterLock

    port = build_root_bound_community_write_lock_port(tmp_path)
    writer = KGSingleWriterLock(write_lock_port=port)
    acquired = writer.acquire(
        board_id="board-1",
        operation="recovery-test",
        owner_id="owner-1",
        ttl_seconds=30,
        admin_lane=True,
    )
    assert acquired.acquired and acquired.owner_token
    result: list[bool] = []
    thread = threading.Thread(
        target=lambda: result.append(
            writer.renew(
                board_id="board-1",
                owner_token=str(acquired.owner_token),
                ttl_seconds=30,
            )
        )
    )
    thread.start()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert result == [True]
    assert writer.release(
        board_id="board-1",
        owner_token=str(acquired.owner_token),
    )
    assert not (tmp_path / "locks" / "board-1" / ".write.lock").exists()


def test_af15_root_bound_write_lock_refuses_path_aliases(tmp_path: Path) -> None:
    from okto_pulse.core.kg.single_writer_lock import KGSingleWriterLock

    with pytest.raises(ValueError, match="absolute kg_base_dir"):
        CommunityLocalWriteLockPort(kg_base_dir="relative-root")

    port = build_root_bound_community_write_lock_port(tmp_path)
    writer = KGSingleWriterLock(write_lock_port=port)
    with pytest.raises(ValueError, match="safe logical identifier"):
        writer.acquire(
            board_id="../outside",
            operation="recovery-test",
            owner_id="owner-1",
        )
    for board_alias in ("board.", "board ", ".. ", "...", "C:", "board:stream", "NUL"):
        with pytest.raises(ValueError, match="board path alias"):
            writer.acquire(
                board_id=board_alias,
                operation="recovery-test",
                owner_id="owner-1",
            )

    aliased = KGSingleWriterLock(base_dir=tmp_path / "other", write_lock_port=port)
    with pytest.raises(ValueError, match="refuses caller path overrides"):
        aliased.acquire(
            board_id="board-1",
            operation="recovery-test",
            owner_id="owner-1",
        )


def test_af15_root_bound_write_lock_refuses_link_aliases(tmp_path: Path) -> None:
    from okto_pulse.core.kg.single_writer_lock import KGSingleWriterLock

    target_root = tmp_path / "target-root"
    target_root.mkdir()
    root_alias = tmp_path / "root-alias"
    try:
        root_alias.symlink_to(target_root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    with pytest.raises(ValueError, match="kg_base_dir alias"):
        build_root_bound_community_write_lock_port(root_alias)

    bound_root = tmp_path / "bound-root"
    locks_root = bound_root / "locks"
    real_board = bound_root / "real-board"
    locks_root.mkdir(parents=True)
    real_board.mkdir()
    (locks_root / "board-1").symlink_to(real_board, target_is_directory=True)
    writer = KGSingleWriterLock(
        write_lock_port=build_root_bound_community_write_lock_port(bound_root)
    )
    with pytest.raises(ValueError, match="board path alias"):
        writer.acquire(
            board_id="board-1",
            operation="recovery-test",
            owner_id="owner-1",
        )


def test_af15_core_cleanup_retries_writer_release_during_reservation_heartbeat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.core.kg.rebuild_service import _RebuildLeaseHeartbeat
    from okto_pulse.core.kg.single_writer_lock import (
        KGAdministrativeOperationReservation,
        KGSingleWriterLock,
    )

    board_id = "board-cleanup-race"
    port = build_root_bound_community_write_lock_port(tmp_path)
    writer = KGSingleWriterLock(write_lock_port=port)
    reservation = KGAdministrativeOperationReservation(write_lock_port=port)
    reserved = reservation.acquire(
        board_id=board_id,
        operation="reservation",
        owner_id="owner-1",
        ttl_seconds=30,
        admin_lane=True,
    )
    acquired = writer.acquire(
        board_id=board_id,
        operation="writer",
        owner_id="owner-1",
        ttl_seconds=30,
        admin_lane=True,
    )
    assert reserved.acquired and reserved.owner_token
    assert acquired.acquired and acquired.owner_token

    real_mutex = port._single_writer_recovery_mutex  # noqa: SLF001
    main_thread = threading.current_thread()
    heartbeat_holds_mutex = threading.Event()
    release_heartbeat = threading.Event()
    held_once = threading.Event()

    class _ControlledMutex:
        def __init__(self, board_dir: Path) -> None:
            self._inner = real_mutex(board_dir)

        def __enter__(self):
            entered = self._inner.__enter__()
            if threading.current_thread() is not main_thread and not held_once.is_set():
                held_once.set()
                heartbeat_holds_mutex.set()
                assert release_heartbeat.wait(timeout=5)
            return entered

        def __exit__(self, *args):
            return self._inner.__exit__(*args)

    monkeypatch.setattr(
        port,
        "_single_writer_recovery_mutex",
        lambda board_dir: _ControlledMutex(board_dir),
    )
    heartbeat = _RebuildLeaseHeartbeat(
        lambda: reservation.renew(
            board_id=board_id,
            owner_token=str(reserved.owner_token),
            ttl_seconds=30,
        ),
        board_id=board_id,
        interval_seconds=0.01,
    )
    release_timer: threading.Timer | None = None
    stop_timer: threading.Timer | None = None
    try:
        heartbeat.start()
        assert heartbeat_holds_mutex.wait(timeout=5)
        release_timer = threading.Timer(0.08, release_heartbeat.set)
        stop_timer = threading.Timer(0.1, heartbeat.stop)
        release_timer.start()
        stop_timer.start()

        assert writer.release(
            board_id=board_id,
            owner_token=str(acquired.owner_token),
        )
        stop_timer.join(timeout=5)
        assert not stop_timer.is_alive()
        assert writer.inspect(board_id=board_id) is None
        assert reservation.release(
            board_id=board_id,
            owner_token=str(reserved.owner_token),
        )
        assert reservation.inspect(board_id=board_id) is None
    finally:
        release_heartbeat.set()
        heartbeat.stop()
        if release_timer is not None:
            release_timer.join(timeout=5)
        if stop_timer is not None:
            stop_timer.join(timeout=5)
        current_writer = writer.inspect(board_id=board_id)
        if current_writer is not None:
            writer.release(
                board_id=board_id,
                owner_token=current_writer.owner_token,
            )
        current_reservation = reservation.inspect(board_id=board_id)
        if current_reservation is not None:
            reservation.release(
                board_id=board_id,
                owner_token=current_reservation.owner_token,
            )
