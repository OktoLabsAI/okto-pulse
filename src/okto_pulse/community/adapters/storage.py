"""Community filesystem storage adapter (spec R05-B, Onda A).

Implements the core ``StorageProvider`` port with the SAME save/load/delete
semantics as ``core.infra.storage.FileSystemStorageProvider`` — extracted to the
Community edition so the core concrete can be retired in R05-E
(register-before-remove). Imports only the PORT (abstract base), never the core
concrete adapter.
"""

from __future__ import annotations

import secrets
from pathlib import Path

from okto_pulse.core.infra.storage import StorageProvider


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


__all__ = ["CommunityFileSystemStorage"]
