"""Mechanism-backed source identity for terminal-debt evidence.

Core receives only opaque SHA-256 fingerprints.  Community derives those
fingerprints from the concrete SQLAlchemy engine or SQLite file identity and
owns the byte-level snapshot provenance used by the local proof runner.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from okto_pulse.core.domain.quality_canonicalization import canonical_sha256


SQLITE_SNAPSHOT_PROVENANCE_SCHEMA = "sqlite-terminal-debt-snapshot/v1"


class TerminalDebtSourceIdentityError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _resolved_file(value: str | os.PathLike[str]) -> Path:
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise TerminalDebtSourceIdentityError("source_identity_unproven") from exc
    if not path.is_file():
        raise TerminalDebtSourceIdentityError("source_identity_unproven")
    return path


def sqlite_file_digest(value: str | os.PathLike[str]) -> str:
    path = _resolved_file(value)
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sqlite_storage_fingerprint(value: str | os.PathLike[str]) -> str:
    path = _resolved_file(value)
    stat = path.stat()
    return canonical_sha256(
        {
            "kind": "sqlite-file",
            "resolved_path": os.path.normcase(str(path)),
            "device": int(stat.st_dev),
            "inode": int(stat.st_ino),
        }
    )


def _session_bind(session_factory: Any) -> Any:
    keywords = getattr(session_factory, "kw", None)
    if isinstance(keywords, dict) and keywords.get("bind") is not None:
        return keywords["bind"]
    bind = getattr(session_factory, "bind", None)
    if bind is not None:
        return bind
    raise TerminalDebtSourceIdentityError("source_identity_unproven")


def _sqlite_database_path(database: object) -> Path | None:
    if not isinstance(database, str) or not database or database == ":memory:":
        return None
    raw = unquote(database)
    if raw.startswith("file:"):
        raw = raw[5:].split("?", 1)[0]
    return _resolved_file(raw)


def sqlalchemy_source_fingerprint(session_factory: Any) -> str:
    """Derive a source fingerprint from the bound engine, never a caller label."""

    bind = _session_bind(session_factory)
    url = getattr(bind, "url", None)
    if url is None:
        sync_engine = getattr(bind, "sync_engine", None)
        url = getattr(sync_engine, "url", None)
    if url is None:
        raise TerminalDebtSourceIdentityError("source_identity_unproven")

    backend = str(url.get_backend_name())
    if backend == "sqlite":
        path = _sqlite_database_path(getattr(url, "database", None))
        if path is not None:
            return sqlite_storage_fingerprint(path)

    # In-memory SQLite and non-file test engines have no filesystem inode.
    # The engine object is the concrete backing-store identity for this
    # process; two labels around the same engine therefore cannot evade alias
    # detection, while distinct engines receive distinct fingerprints.
    rendered = str(url.render_as_string(hide_password=True))
    return canonical_sha256(
        {
            "kind": "sqlalchemy-engine",
            "backend": backend,
            "url": rendered,
            "engine_instance": f"{id(bind):x}",
        }
    )


_ATTESTATION_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False)
class SqliteTerminalDebtSnapshotIsolation:
    origin_path: Path
    copy_path: Path
    origin_fingerprint: str
    copy_fingerprint: str
    baseline_content_digest: str
    provenance_digest: str
    _attested: object

    def __init__(
        self,
        *,
        origin_path: Path,
        copy_path: Path,
        origin_fingerprint: str,
        copy_fingerprint: str,
        baseline_content_digest: str,
        provenance_digest: str,
        _token: object,
    ) -> None:
        if _token is not _ATTESTATION_TOKEN:
            raise TerminalDebtSourceIdentityError("source_identity_unproven")
        object.__setattr__(self, "origin_path", origin_path)
        object.__setattr__(self, "copy_path", copy_path)
        object.__setattr__(self, "origin_fingerprint", origin_fingerprint)
        object.__setattr__(self, "copy_fingerprint", copy_fingerprint)
        object.__setattr__(self, "baseline_content_digest", baseline_content_digest)
        object.__setattr__(self, "provenance_digest", provenance_digest)
        object.__setattr__(self, "_attested", _ATTESTATION_TOKEN)

    def verify_pre_execution(self) -> None:
        if self._attested is not _ATTESTATION_TOKEN:
            raise TerminalDebtSourceIdentityError("source_identity_unproven")
        try:
            if os.path.samefile(self.origin_path, self.copy_path):
                raise TerminalDebtSourceIdentityError("origin_copy_alias")
        except OSError as exc:
            raise TerminalDebtSourceIdentityError("source_identity_unproven") from exc
        if sqlite_storage_fingerprint(self.origin_path) != self.origin_fingerprint:
            raise TerminalDebtSourceIdentityError("source_identity_unproven")
        if sqlite_storage_fingerprint(self.copy_path) != self.copy_fingerprint:
            raise TerminalDebtSourceIdentityError("source_identity_unproven")
        if sqlite_file_digest(self.origin_path) != self.baseline_content_digest:
            raise TerminalDebtSourceIdentityError("origin_changed_before_execution")
        if sqlite_file_digest(self.copy_path) != self.baseline_content_digest:
            raise TerminalDebtSourceIdentityError("copy_changed_before_execution")


def attest_sqlite_terminal_debt_snapshot(
    *,
    origin_path: str | os.PathLike[str],
    copy_path: str | os.PathLike[str],
) -> SqliteTerminalDebtSnapshotIsolation:
    """Attest two existing byte-identical SQLite files as origin and copy."""

    origin = _resolved_file(origin_path)
    copy = _resolved_file(copy_path)
    try:
        if os.path.samefile(origin, copy):
            raise TerminalDebtSourceIdentityError("origin_copy_alias")
    except OSError as exc:
        raise TerminalDebtSourceIdentityError("source_identity_unproven") from exc

    origin_fingerprint = sqlite_storage_fingerprint(origin)
    copy_fingerprint = sqlite_storage_fingerprint(copy)
    if origin_fingerprint == copy_fingerprint:
        raise TerminalDebtSourceIdentityError("origin_copy_alias")
    origin_digest = sqlite_file_digest(origin)
    if sqlite_file_digest(copy) != origin_digest:
        raise TerminalDebtSourceIdentityError("copy_baseline_mismatch")
    provenance_digest = canonical_sha256(
        {
            "schema_version": SQLITE_SNAPSHOT_PROVENANCE_SCHEMA,
            "origin_fingerprint": origin_fingerprint,
            "copy_fingerprint": copy_fingerprint,
            "baseline_content_digest": origin_digest,
        }
    )
    return SqliteTerminalDebtSnapshotIsolation(
        origin_path=origin,
        copy_path=copy,
        origin_fingerprint=origin_fingerprint,
        copy_fingerprint=copy_fingerprint,
        baseline_content_digest=origin_digest,
        provenance_digest=provenance_digest,
        _token=_ATTESTATION_TOKEN,
    )


__all__ = [
    "SQLITE_SNAPSHOT_PROVENANCE_SCHEMA",
    "SqliteTerminalDebtSnapshotIsolation",
    "TerminalDebtSourceIdentityError",
    "attest_sqlite_terminal_debt_snapshot",
    "sqlalchemy_source_fingerprint",
    "sqlite_file_digest",
    "sqlite_storage_fingerprint",
]
