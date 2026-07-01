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

import os
import secrets
from collections.abc import AsyncIterator
from pathlib import Path

import anyio

from okto_pulse.core.infra.storage import (
    DEFAULT_STREAM_CHUNK_SIZE,
    StorageObjectStat,
    StorageProvider,
)


class CommunityFileSystemStorage(StorageProvider):
    """Local filesystem storage provider (Community edition)."""

    def __init__(self, base_dir: str) -> None:
        self.base_dir = Path(base_dir)

    async def save(self, board_id: str, filename: str, content: bytes) -> str:
        safe_name = Path(filename).name
        unique_name = f"{secrets.token_hex(8)}_{safe_name}"
        upload_dir = self.base_dir / board_id
        upload_dir.mkdir(parents=True, exist_ok=True)
        file_path = upload_dir / unique_name
        file_path.write_bytes(content)
        return str(file_path)

    async def load(self, path: str) -> bytes:
        return Path(path).read_bytes()

    async def delete(self, path: str) -> bool:
        try:
            Path(path).unlink()
            return True
        except FileNotFoundError:
            return False

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
                to_read = chunk_size if remaining is None else min(chunk_size, remaining)
                if to_read <= 0:
                    break
                chunk = await handle.read(to_read)
                if not chunk:
                    break
                if remaining is not None:
                    remaining -= len(chunk)
                yield chunk


__all__ = ["CommunityFileSystemStorage"]
