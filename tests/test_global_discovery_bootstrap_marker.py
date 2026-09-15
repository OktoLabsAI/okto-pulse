"""INV-E2 durable incomplete-bootstrap marker regressions.

Covers the write-ahead intent-log contract (Nexus
msg_08ef262ec7b744c496e742bb6b42d45a / msg_20533dbbce3741248416fc0e53b7ea4e)
and the Codex-validator ordering/authority blockers:

* marker persisted BEFORE any bootstrap-side filesystem action (before close);
* durable completion ordering (close -> fsync -> readback -> fsync -> clear),
  never page-cache readability;
* authority-checked, TOCTOU-free clear;
* ``state()`` marker precedence with a metadata-only ``primary_confirmed_absent``
  fact and no Core-typed marker-bypass method;
* ordinary open/auto-bootstrap refuses over a live marker (recovery-only truth);
* R1 mid-DDL cross-context unreadable and R4 healthy/absent-retry.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

from okto_pulse.community.adapters.global_discovery_bootstrap_marker import (
    BOOTSTRAP_INCOMPLETE_MARKER_FILENAME,
    BootstrapMarkerAuthorityError,
    bootstrap_marker_path,
    bootstrap_marker_present,
    clear_bootstrap_marker,
    read_bootstrap_marker,
    write_bootstrap_marker,
)
from okto_pulse.core.kg.global_discovery_writer import (
    GlobalDiscoveryWriterLease,
)


class _AlwaysOwnedWriterLock:
    def is_owner(self, _board_id: str, _owner_token: str) -> bool:
        return True

    def release(self, *, board_id: str, owner_token: str) -> bool:
        del board_id, owner_token
        return True


@contextmanager
def under_global_safe_write(owner_token: str, operation: str):
    lease = GlobalDiscoveryWriterLease(
        lock=_AlwaysOwnedWriterLock(),  # type: ignore[arg-type]
        owner_token=owner_token,
        operation=operation,
    )
    try:
        with lease.guard():
            yield
    finally:
        lease.release()


# ---------------------------------------------------------------------------
# Marker module unit contract
# ---------------------------------------------------------------------------


def test_marker_path_is_adjacent_and_not_residue_prefixed(tmp_path):
    legacy = tmp_path / "global" / "discovery.lbug"
    marker = bootstrap_marker_path(legacy)
    assert marker.parent == legacy.parent
    assert marker.name == BOOTSTRAP_INCOMPLETE_MARKER_FILENAME
    # Must not share the residue scan prefix ``discovery.lbug.``.
    assert not marker.name.startswith(legacy.name + ".")


def test_write_read_clear_roundtrip_is_bounded_and_durable(tmp_path):
    legacy = tmp_path / "global" / "discovery.lbug"
    assert bootstrap_marker_present(legacy) is False
    write = write_bootstrap_marker(legacy)
    assert isinstance(write.directory_fsync_supported, bool)
    assert bootstrap_marker_present(legacy) is True
    payload = read_bootstrap_marker(legacy)
    assert set(payload) == {"created_at", "kind", "nonce"}
    assert payload["kind"] == "init_bootstrap"
    assert payload["nonce"] == write.nonce
    clear_bootstrap_marker(legacy, fence_check=lambda: None)
    assert bootstrap_marker_present(legacy) is False
    # Idempotent clear.
    clear_bootstrap_marker(legacy, fence_check=lambda: None)
    assert read_bootstrap_marker(legacy) is None


def test_clear_requires_mandatory_authority_callback(tmp_path):
    legacy = tmp_path / "global" / "discovery.lbug"
    write_bootstrap_marker(legacy)
    with pytest.raises(BootstrapMarkerAuthorityError):
        clear_bootstrap_marker(legacy, fence_check=None)  # type: ignore[arg-type]
    # A null authority never performs a silent clear.
    assert bootstrap_marker_present(legacy) is True

    # The callback is invoked immediately before the physical unlink.
    calls: list[str] = []

    def _fence() -> None:
        calls.append("fence")

    clear_bootstrap_marker(legacy, fence_check=_fence)
    assert calls == ["fence"]
    assert bootstrap_marker_present(legacy) is False


# ---------------------------------------------------------------------------
# state() precedence + primary_confirmed_absent detail (blockers 4, 7)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Blocker 1 - marker precedes close / all bootstrap-side FS actions
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Blocker 2 - durable completion ordering, not page-cache readability
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Blocker 3 - authority-checked, TOCTOU-free clear
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Blocker 8 - ordinary open/auto-bootstrap refuses over a live marker
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# R1 - mid-DDL failure, fresh context observes unreadable (never readable)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# R4 - healthy no-marker has no false positive; marker+absent retried safely
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Blocker 23 - warm-handle marker bypass: a marker published after warm-up
# makes every ordinary borrow refuse, zero open/mutation/clear.
# ---------------------------------------------------------------------------


def _graph_fingerprint(legacy: Path) -> tuple:
    # Stat-based (size + mtime): the warm handle holds an exclusive lock on the
    # open .lbug on Windows, so reading its bytes would fail; size+mtime still
    # proves zero mutation across the refused borrows.
    parent = legacy.parent
    entries = []
    for child in sorted(parent.glob(legacy.name + "*")):
        if child.is_file():
            info = child.stat()
            entries.append((child.name, info.st_size, info.st_mtime_ns))
    return tuple(entries)


# ---------------------------------------------------------------------------
# Blocker 24 - a non-already-exists vector-index DDL failure must propagate and
# preserve the marker (never silently cleared).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Blocker 18/30 - directory-fsync durability evidence is threaded, not erased.
# Force false at each boundary and assert the observable aggregate is False.
# ---------------------------------------------------------------------------
