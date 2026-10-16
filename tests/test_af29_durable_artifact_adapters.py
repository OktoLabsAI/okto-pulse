from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


from okto_pulse.community.adapters.coordination import CommunityLocalWriteLockPort
from okto_pulse.community.adapters.rebuild_audit_storage import (
    CommunityFileSystemCognitivePendingWorkProvider,
)
from okto_pulse.core.kg.global_discovery_writer import GlobalDiscoveryWriterLease
from okto_pulse.core.kg.single_writer_lock import KGSingleWriterLock


@contextmanager
def _durable_global_writer(
    tmp_path: Path,
    *,
    operation: str,
) -> Iterator[None]:
    lease = GlobalDiscoveryWriterLease.acquire(
        operation=operation,
        lock=KGSingleWriterLock(
            base_dir=tmp_path / "locks",
            write_lock_port=CommunityLocalWriteLockPort(),
        ),
    )
    try:
        with lease.guard():
            yield
    finally:
        lease.release()


def test_af29_cognitive_pending_provider_enumerates_local_ledgers(tmp_path):
    provider = CommunityFileSystemCognitivePendingWorkProvider(tmp_path)
    board_a = tmp_path / "rebuild" / "audit" / "cognitive_pending" / "board-a"
    board_b = tmp_path / "rebuild" / "audit" / "cognitive_pending" / "board-b"
    board_a.mkdir(parents=True)
    board_b.mkdir(parents=True)
    (board_a / "gen-2.json").write_text("{not-json", encoding="utf-8")
    (board_a / "gen-1.json").write_text("{}", encoding="utf-8")
    (board_a / "ignore.txt").write_text("{}", encoding="utf-8")
    (board_b / "gen-3.json").write_text("{}", encoding="utf-8")

    records = provider.list_records()

    assert [(r.board_id, r.kg_generation_id) for r in records] == [
        ("board-a", "gen-1"),
        ("board-a", "gen-2"),
        ("board-b", "gen-3"),
    ]
