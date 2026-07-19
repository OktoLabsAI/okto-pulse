"""Durable write-ahead intent log for Global Discovery bootstrap.

INV-E2 requires *cross-process* physical evidence that a bootstrap began but
did not finish.  A process-memory latch (``_corrupt_open_latch``) cannot satisfy
this: it vanishes when the failed ``init`` process exits, so a fresh runtime
would classify a half-written legacy graph as readable.

This module persists a small adjacent marker file *before* any bootstrap
mutation.  Its mere existence — observed metadata-only by
``CommunityGlobalDiscoveryRuntime.state()`` — forces
``PRESENT_UNREADABLE_OR_ERROR`` / ``global_discovery_bootstrap_incomplete`` /
``quarantined=True``, overriding legacy/pointer presence.  Only a durably
complete bootstrap (primary written *and* read back) removes it, and only the
fenced physical recovery adapter may clear it after a valid cutover.

The marker reuses the exact atomic ``temp + rename + directory-fsync`` writer
(:func:`write_json_atomic`) that the recovery journal uses; it does not invent a
second persistence protocol.  Content is intentionally bounded to
``created_at``, ``kind`` and a per-incarnation ``nonce``.
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, NamedTuple

from okto_pulse.community.adapters.global_discovery_layout import (
    fsync_directory,
    write_json_atomic,
)


class BootstrapMarkerWrite(NamedTuple):
    """Durable-write evidence for the incomplete-bootstrap marker.

    ``directory_fsync_supported`` is the boolean returned by ``write_json_atomic``
    and must be threaded by callers rather than silently dropped (blocker 18/30).
    """

    nonce: str
    directory_fsync_supported: bool

logger = logging.getLogger(
    "okto_pulse.community.global_discovery_bootstrap_marker"
)

BOOTSTRAP_INCOMPLETE_MARKER_FILENAME = "discovery_bootstrap_incomplete.json"
BOOTSTRAP_MARKER_KIND = "init_bootstrap"
BOOTSTRAP_INCOMPLETE_REASON = "global_discovery_bootstrap_incomplete"


class BootstrapMarkerAuthorityError(RuntimeError):
    """Raised when a marker clear is attempted without a live authority callback.

    Absence of a fence/authority callback is a typed composition error, never a
    silent clear (Nexus msg_08f6fa2df8ab4e728144e12e30ca7c67).
    """

    code = "global_discovery_bootstrap_marker_clear_unauthorized"


def bootstrap_marker_path(legacy_path: Path) -> Path:
    """Return the marker path adjacent to the legacy global graph.

    The marker lives in the same ``global/`` directory as the legacy graph and
    is therefore stable across generations (recovery cutover moves the graph
    into ``discovery.generations/`` but never the marker).  Its name does not
    share the ``discovery.lbug.`` residue prefix, so the residue scanner never
    mistakes it for a partial primary.
    """

    return legacy_path.parent / BOOTSTRAP_INCOMPLETE_MARKER_FILENAME


def write_bootstrap_marker(
    legacy_path: Path, *, nonce: str | None = None
) -> BootstrapMarkerWrite:
    """Atomically persist the incomplete-bootstrap marker before any mutation.

    Returns the nonce/incarnation written and the directory-fsync support flag
    from :func:`write_json_atomic` (temp + rename + directory fsync), the same
    mechanism the recovery journal writer uses.  Callers must thread the
    durability flag (blocker 18/30) rather than discard it.
    """

    incarnation = nonce or secrets.token_hex(8)
    path = bootstrap_marker_path(legacy_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    directory_fsync_supported = write_json_atomic(
        path,
        {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "kind": BOOTSTRAP_MARKER_KIND,
            "nonce": incarnation,
        },
    )
    logger.warning(
        "global_discovery.bootstrap_marker_written",
        extra={
            "event": "global_discovery.bootstrap_marker_written",
            "reason_code": BOOTSTRAP_INCOMPLETE_REASON,
        },
    )
    return BootstrapMarkerWrite(
        nonce=incarnation, directory_fsync_supported=bool(directory_fsync_supported)
    )


def bootstrap_marker_present(legacy_path: Path) -> bool:
    """Metadata-only existence probe.

    ``FileNotFoundError`` means absent.  Any other ``OSError`` is re-raised so
    the caller can classify the underlying stat failure conservatively rather
    than silently treating an unreadable directory as marker-free.
    """

    try:
        bootstrap_marker_path(legacy_path).stat()
    except FileNotFoundError:
        return False
    return True


def read_bootstrap_marker(legacy_path: Path) -> dict | None:
    """Return the marker payload, or ``None`` when absent.

    Used by the recovery ceremony and tests.  ``state()`` deliberately never
    reads content — marker *existence* alone is the fail-closed signal.
    """

    import json

    path = bootstrap_marker_path(legacy_path)
    try:
        with path.open("r", encoding="utf-8") as stream:
            raw = json.load(stream)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:  # unreadable but present => stay closed
        logger.warning(
            "global_discovery.bootstrap_marker_unreadable",
            extra={"event": "global_discovery.bootstrap_marker_unreadable"},
        )
        raise exc
    return raw if isinstance(raw, dict) else None


def clear_bootstrap_marker(
    legacy_path: Path,
    *,
    fence_check: Callable[[], None],
) -> bool:
    """Remove the marker and durably fsync its directory (authority-checked).

    ``fence_check`` is mandatory and non-null: a missing callback is a typed
    :class:`BootstrapMarkerAuthorityError`, never a silent clear.  It is invoked
    immediately before the physical unlink so an accidental invocation outside a
    valid guard cannot remove the marker (no TOCTOU from an earlier caller-side
    check).  This is the only unlink path; no raw unauthorised helper is
    exported.

    Idempotent: an already-absent marker is a success.  Returns whether the
    directory fsync reported support (mirroring the layout helpers so callers
    can thread ``directory_fsync_supported`` through their journals).
    """

    if fence_check is None:
        raise BootstrapMarkerAuthorityError(
            "global_discovery_bootstrap_marker_clear_unauthorized: a live "
            "fence/authority callback is required to clear the marker"
        )
    fence_check()
    path = bootstrap_marker_path(legacy_path)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    supported = fsync_directory(path.parent)
    logger.info(
        "global_discovery.bootstrap_marker_cleared",
        extra={"event": "global_discovery.bootstrap_marker_cleared"},
    )
    return supported


__all__ = [
    "BOOTSTRAP_INCOMPLETE_MARKER_FILENAME",
    "BOOTSTRAP_INCOMPLETE_REASON",
    "BOOTSTRAP_MARKER_KIND",
    "BootstrapMarkerAuthorityError",
    "BootstrapMarkerWrite",
    "bootstrap_marker_path",
    "bootstrap_marker_present",
    "clear_bootstrap_marker",
    "read_bootstrap_marker",
    "write_bootstrap_marker",
]
