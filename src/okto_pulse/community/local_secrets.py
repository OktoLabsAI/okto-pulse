"""Stable, installation-local secrets owned by the Community edition."""

from __future__ import annotations

import errno
import os
import secrets
import stat
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import SecretStr

GUIDELINE_POLICY_CURSOR_SECRET_RELATIVE_PATH = (
    Path("secrets") / "guideline-policy-cursor.key"
)
_MINIMUM_CURSOR_SECRET_BYTES = 32
_MAXIMUM_CURSOR_SECRET_FILE_BYTES = 1024


class CommunityLocalSecretError(RuntimeError):
    """A persisted Community secret could not be created or loaded safely."""

    code = "community_local_secret_unavailable"


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("local secret write made no progress")
        remaining = remaining[written:]


def _read_at_most(descriptor: int, maximum_bytes: int) -> bytes:
    chunks: list[bytes] = []
    remaining = maximum_bytes
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _fsync_directory(path: Path) -> None:
    """Durably publish the directory entry where the platform supports it."""

    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        unsupported = {
            errno.EINVAL,
            getattr(errno, "ENOTSUP", errno.EINVAL),
            getattr(errno, "EOPNOTSUPP", errno.EINVAL),
        }
        if exc.errno in unsupported:
            return
        raise CommunityLocalSecretError(
            f"Unable to persist Community secret directory: {path}"
        ) from exc


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise CommunityLocalSecretError(
            f"Community secret directory is not a regular directory: {path}"
        )
    if os.name != "nt":
        try:
            path.chmod(0o700)
        except OSError as exc:
            raise CommunityLocalSecretError(
                f"Unable to secure Community secret directory: {path}"
            ) from exc
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise CommunityLocalSecretError(
                f"Community secret directory permissions are too broad: {path}"
            )


def _publish_generated_secret(path: Path) -> None:
    """Publish a complete secret without replacing an existing installation key."""

    pending = path.with_name(f".{path.name}.pending-{os.getpid()}-{uuid4().hex}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0)
    descriptor: int | None = None
    published = False
    try:
        descriptor = os.open(pending, flags, 0o600)
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        payload = f"{secrets.token_hex(32)}\n".encode("ascii")
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None

        # Both branches refuse an existing destination. This preserves the
        # first complete key when two local processes race during first boot.
        if os.name == "nt":
            os.rename(pending, path)
        else:
            os.link(pending, path, follow_symlinks=False)
            pending.unlink()
        published = True
    except FileExistsError:
        # Another process completed first. Its key is authoritative and will
        # be read and validated by the caller.
        pass
    except OSError as exc:
        raise CommunityLocalSecretError(
            f"Unable to create Community guideline policy cursor secret: {path}"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            pending.unlink(missing_ok=True)
        except OSError:
            # The published key remains authoritative. A stale hidden sibling
            # is inert and can be cleaned on a later maintenance pass.
            pass
    if published:
        _fsync_directory(path.parent)


def _read_secret(path: Path) -> str:
    try:
        path_metadata = path.lstat()
    except OSError as exc:
        raise CommunityLocalSecretError(
            f"Unable to load Community guideline policy cursor secret: {path}"
        ) from exc
    if path.is_symlink() or not stat.S_ISREG(path_metadata.st_mode):
        raise CommunityLocalSecretError(
            f"Community guideline policy cursor secret is not a regular file: {path}"
        )

    if os.name != "nt":
        try:
            path.chmod(0o600)
        except OSError as exc:
            raise CommunityLocalSecretError(
                f"Unable to secure Community guideline policy cursor secret: {path}"
            ) from exc
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise CommunityLocalSecretError(
                f"Community guideline policy cursor secret permissions are too broad: {path}"
            )

    flags = os.O_RDONLY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            opened_metadata = os.fstat(descriptor)
            if not stat.S_ISREG(opened_metadata.st_mode):
                raise CommunityLocalSecretError(
                    "Community guideline policy cursor secret changed while opening"
                )
            raw = _read_at_most(
                descriptor,
                _MAXIMUM_CURSOR_SECRET_FILE_BYTES + 1,
            )
        finally:
            os.close(descriptor)
    except CommunityLocalSecretError:
        raise
    except OSError as exc:
        raise CommunityLocalSecretError(
            f"Unable to load Community guideline policy cursor secret: {path}"
        ) from exc

    if len(raw) > _MAXIMUM_CURSOR_SECRET_FILE_BYTES:
        raise CommunityLocalSecretError(
            f"Community guideline policy cursor secret is invalid: {path}"
        )
    try:
        value = raw.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise CommunityLocalSecretError(
            f"Community guideline policy cursor secret is invalid: {path}"
        ) from exc
    if len(value.encode("ascii")) < _MINIMUM_CURSOR_SECRET_BYTES:
        raise CommunityLocalSecretError(
            f"Community guideline policy cursor secret is invalid: {path}"
        )
    return value


def provision_guideline_policy_cursor_signing_key(settings: Any) -> Path | None:
    """Hydrate the Community settings snapshot with one stable local key.

    Explicit configuration (normally
    ``GUIDELINE_POLICY_CURSOR_SIGNING_KEY``) always wins and is never copied
    into the installation directory. Without an override, the key is generated
    once under ``data_dir`` and reused across API and MCP restarts.
    """

    if getattr(settings, "guideline_policy_cursor_signing_key", None) is not None:
        return None

    data_dir = Path(settings.data_dir).expanduser().resolve()
    secret_path = data_dir / GUIDELINE_POLICY_CURSOR_SECRET_RELATIVE_PATH
    _ensure_private_directory(secret_path.parent)
    if not os.path.lexists(secret_path):
        _publish_generated_secret(secret_path)

    value = _read_secret(secret_path)
    settings.guideline_policy_cursor_signing_key = SecretStr(value)
    return secret_path


__all__ = [
    "CommunityLocalSecretError",
    "GUIDELINE_POLICY_CURSOR_SECRET_RELATIVE_PATH",
    "provision_guideline_policy_cursor_signing_key",
]
