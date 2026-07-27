"""Community filesystem storage adapter (spec R05-B, Onda A).

Implements the core ``StorageProvider`` port with the SAME save/load/delete
semantics as ``core.infra.storage.FileSystemStorageProvider`` — extracted to the
Community edition so the core concrete can be retired in R05-E
(register-before-remove). Imports only the PORT (abstract base), never the core
concrete adapter.

R02 (AC6): the read side gains an OFFLOADED, CHUNKED implementation of the new
``stat`` + ``open_stream`` port methods. ``stat`` runs ``os.stat`` in a worker
thread; ``open_stream`` reads the file in chunks through ``anyio.open_file`` (a
worker thread per blocking read) WITHOUT materialising the whole file on the
request path — so large downloads never block the API event loop. ``anyio`` is
already a transitive dependency (Starlette/FastAPI); this adds no new core
filesystem dependency (TR4 governs the CORE, not this Community adapter).
"""

from __future__ import annotations

import hashlib
import os
import secrets
from collections.abc import AsyncIterator
from pathlib import Path

import anyio
from filelock import FileLock

from okto_pulse.core import (
    DEFAULT_STREAM_CHUNK_SIZE,
    StorageObjectStat,
    StorageProvider,
)

from .filesystem_erasure import (
    fsync_directory,
    remove_contained_tree,
    validate_scope_id,
)


class CommunityFileSystemStorage(StorageProvider):
    """Local filesystem storage provider (Community edition)."""

    def __init__(self, base_dir: str) -> None:
        self.base_dir = Path(base_dir).expanduser().resolve(strict=False)

    def _lifecycle_paths(self, board_id: str) -> tuple[Path, Path]:
        digest = hashlib.sha256(board_id.encode("utf-8")).hexdigest()
        control_dir = self.base_dir / ".board_lifecycle"
        return (
            control_dir / f"{digest}.lock",
            control_dir / f"{digest}.erased",
        )

    @staticmethod
    def _write_erasure_marker(marker: Path, *, board_id: str) -> None:
        del board_id
        if marker.exists():
            return
        marker.parent.mkdir(parents=True, exist_ok=True)
        temporary = marker.with_name(f".{marker.name}.{secrets.token_hex(8)}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(b"erased\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, marker)
            fsync_directory(marker.parent)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    async def save(self, board_id: str, filename: str, content: bytes) -> str:
        safe_board_id = validate_scope_id(board_id)
        safe_name = Path(filename).name
        unique_name = f"{secrets.token_hex(8)}_{safe_name}"
        lock_path, erasure_marker = self._lifecycle_paths(safe_board_id)

        def _save() -> str:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with FileLock(str(lock_path), timeout=30):
                if erasure_marker.exists():
                    raise RuntimeError(
                        "attachment storage is permanently erased for board: "
                        f"{safe_board_id}"
                    )
                upload_dir = self.base_dir / safe_board_id
                upload_dir.mkdir(parents=True, exist_ok=True)
                file_path = upload_dir / unique_name
                temporary = upload_dir / (
                    f".{unique_name}.{secrets.token_hex(8)}.upload"
                )
                try:
                    with temporary.open("xb") as handle:
                        handle.write(content)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, file_path)
                    fsync_directory(upload_dir)
                finally:
                    try:
                        temporary.unlink()
                    except FileNotFoundError:
                        pass
                return str(file_path)

        return await anyio.to_thread.run_sync(_save)

    async def load(self, path: str) -> bytes:
        return Path(path).read_bytes()

    async def delete(self, path: str) -> bool:
        target = Path(path).expanduser().resolve(strict=False)
        try:
            relative = target.relative_to(self.base_dir)
        except ValueError as exc:
            raise ValueError("attachment delete path escapes storage root") from exc
        if len(relative.parts) != 2:
            raise ValueError("attachment delete path has invalid object scope")
        safe_board_id = validate_scope_id(relative.parts[0])
        lock_path, _erasure_marker = self._lifecycle_paths(safe_board_id)

        def _delete() -> bool:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with FileLock(str(lock_path), timeout=30):
                try:
                    target.unlink()
                except FileNotFoundError:
                    return False
                fsync_directory(target.parent)
                return True

        return await anyio.to_thread.run_sync(_delete)

    async def restore(self, path: str, content: bytes) -> None:
        """Restore one deleted attachment under its board lifecycle fence.

        This exact-path operation exists only for compensating a relational
        commit failure. It cannot escape ``base_dir``, write lifecycle control
        files, revive an erased board, or overwrite different live content.
        """

        target = Path(path).expanduser().resolve(strict=False)
        try:
            relative = target.relative_to(self.base_dir)
        except ValueError as exc:
            raise ValueError("attachment restore path escapes storage root") from exc
        if len(relative.parts) != 2:
            raise ValueError("attachment restore path has invalid object scope")
        safe_board_id = validate_scope_id(relative.parts[0])
        lock_path, erasure_marker = self._lifecycle_paths(safe_board_id)

        def _restore() -> None:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with FileLock(str(lock_path), timeout=30):
                if erasure_marker.exists():
                    raise RuntimeError(
                        "attachment storage is permanently erased for board: "
                        f"{safe_board_id}"
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    if target.read_bytes() == content:
                        return
                    raise FileExistsError(
                        "attachment restore refused to overwrite live content"
                    )
                temporary = target.with_name(
                    f".{target.name}.{secrets.token_hex(8)}.restore"
                )
                try:
                    with temporary.open("xb") as handle:
                        handle.write(content)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, target)
                    fsync_directory(target.parent)
                finally:
                    try:
                        temporary.unlink()
                    except FileNotFoundError:
                        pass

        await anyio.to_thread.run_sync(_restore)

    async def purge_board(self, board_id: str) -> dict[str, object]:
        """Physically remove one board's upload namespace.

        The logical identifier is validated before constructing a path, and the
        recursive remover never follows symlinks or Windows junctions. Deletion
        is offloaded because a board may own many attachments.
        """

        safe_board_id = validate_scope_id(board_id)

        def _purge() -> dict[str, object]:
            lock_path, erasure_marker = self._lifecycle_paths(safe_board_id)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with FileLock(str(lock_path), timeout=30):
                # Persist the non-sensitive lifecycle tombstone first. A save
                # racing this purge shares the same cross-process lock and can
                # therefore never recreate the board namespace after absence
                # has been verified.
                self._write_erasure_marker(
                    erasure_marker,
                    board_id=safe_board_id,
                )
                board_root = self.base_dir / safe_board_id
                files_removed, directories_removed = remove_contained_tree(
                    board_root,
                    base_dir=self.base_dir,
                )
                try:
                    board_root.lstat()
                except FileNotFoundError:
                    verified_absent = True
                else:
                    verified_absent = False
                if not verified_absent:
                    raise RuntimeError(
                        "attachment board namespace remained after physical erasure: "
                        f"{safe_board_id}"
                    )
                fsync_directory(self.base_dir)
            return {
                "board_id": safe_board_id,
                "objects_removed": files_removed,
                "directories_removed": directories_removed,
                "verified_absent": True,
                "status": (
                    "purged" if files_removed or directories_removed else "not_found"
                ),
            }

        return await anyio.to_thread.run_sync(_purge)

    async def stat(self, path: str) -> StorageObjectStat:
        """Offloaded ``os.stat`` — size + mtime so the core reproduces the baseline
        ``Last-Modified``/``ETag`` headers without touching the path itself. Raises
        ``FileNotFoundError`` when the file is gone (core maps it to a 404)."""
        st = await anyio.to_thread.run_sync(os.stat, path)
        return StorageObjectStat(size=st.st_size, modified_time=st.st_mtime)

    async def open_stream(
        self,
        path: str,
        *,
        start: int = 0,
        end: int | None = None,
        chunk_size: int = DEFAULT_STREAM_CHUNK_SIZE,
    ) -> AsyncIterator[bytes]:
        """Stream ``[start:end)`` in ``chunk_size`` chunks via ``anyio.open_file``.

        Each ``read`` is offloaded to a worker thread, so a large download never
        blocks the event loop, and the whole file is NEVER held in memory at once
        (AC6 — streaming/chunks, no whole-file materialisation). Raises
        ``FileNotFoundError`` when the file is absent.
        """
        async with await anyio.open_file(path, "rb") as handle:
            if start:
                await handle.seek(start)
            remaining = None if end is None else max(0, end - start)
            while True:
                to_read = (
                    chunk_size if remaining is None else min(chunk_size, remaining)
                )
                if to_read <= 0:
                    break
                chunk = await handle.read(to_read)
                if not chunk:
                    break
                if remaining is not None:
                    remaining -= len(chunk)
                yield chunk


__all__ = ["CommunityFileSystemStorage"]
