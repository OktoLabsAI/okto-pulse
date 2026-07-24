"""Compensating-restore contract for ``CommunityFileSystemStorage``.

The governed board-deletion compensation path (a relational commit failure after
an attachment was physically deleted) calls ``CommunityFileSystemStorage.restore``
to put the deleted object back EXACTLY where it was. These tests pin that contract
without touching the implementation:

1. save -> load -> delete -> restore returns the exact bytes at the exact path;
2. restore is idempotent for identical bytes (and refuses to clobber different
   live content);
3. restore refuses any path outside ``base_dir`` (escape / traversal / wrong
   object scope);
4. after ``purge_board`` writes the erasure marker, restore fails CLOSED and does
   NOT recreate the board namespace.

The storage methods are async; they are driven through ``asyncio.run`` so this
file is hermetic and independent of any pytest-asyncio configuration.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from okto_pulse.community.adapters.storage import CommunityFileSystemStorage


def test_save_load_delete_restore_preserves_bytes_and_path(tmp_path) -> None:
    storage = CommunityFileSystemStorage(str(tmp_path / "uploads"))

    async def scenario() -> None:
        content = b"exact-attachment-bytes-\x00\x01\x02\xff"
        path = await storage.save("board-round", "note.bin", content)
        assert await storage.load(path) == content
        assert await storage.delete(path) is True
        assert not Path(path).exists()

        await storage.restore(path, content)

        # Same path, same bytes — nothing shifted.
        assert Path(path).exists()
        assert Path(path).read_bytes() == content
        assert await storage.load(path) == content

    asyncio.run(scenario())


def test_restore_is_idempotent_and_refuses_to_clobber(tmp_path) -> None:
    storage = CommunityFileSystemStorage(str(tmp_path / "uploads"))

    async def scenario() -> None:
        content = b"idempotent-bytes"
        path = await storage.save("board-idem", "a.txt", content)
        assert await storage.delete(path) is True

        # First restore recreates the object; further restores with identical
        # bytes are no-ops (no error, bytes unchanged) — over both a missing and
        # an already-present target.
        await storage.restore(path, content)
        await storage.restore(path, content)
        await storage.restore(path, content)
        assert await storage.load(path) == content

        # Restore must NOT overwrite different live content.
        with pytest.raises(FileExistsError):
            await storage.restore(path, b"different-bytes")
        assert await storage.load(path) == content

    asyncio.run(scenario())


def test_restore_rejects_paths_outside_base_dir(tmp_path) -> None:
    base = tmp_path / "uploads"
    storage = CommunityFileSystemStorage(str(base))

    async def scenario() -> None:
        # Sibling directory entirely outside base_dir.
        outside = tmp_path / "outside" / "board" / "evil.txt"
        with pytest.raises(ValueError, match="escapes storage root"):
            await storage.restore(str(outside), b"x")

        # Traversal that resolves above base_dir.
        traversal = base / "board" / ".." / ".." / "escape.txt"
        with pytest.raises(ValueError, match="escapes storage root"):
            await storage.restore(str(traversal), b"x")

        # Under base_dir but not a <board>/<object> path (wrong scope depth).
        wrong_scope = base / "loose-object.txt"
        with pytest.raises(ValueError, match="invalid object scope"):
            await storage.restore(str(wrong_scope), b"x")

        # No stray writes anywhere.
        assert not outside.exists()
        assert not (base / "loose-object.txt").exists()

    asyncio.run(scenario())


def test_restore_after_purge_fails_closed_without_recreating_namespace(
    tmp_path,
) -> None:
    storage = CommunityFileSystemStorage(str(tmp_path / "uploads"))

    async def scenario() -> None:
        content = b"to-be-erased"
        path = await storage.save("board-purge", "f.txt", content)
        assert await storage.delete(path) is True

        result = await storage.purge_board("board-purge")
        assert result["verified_absent"] is True

        board_root = storage.base_dir / "board-purge"
        assert not board_root.exists()

        # The erasure marker is a permanent tombstone: restore fails closed.
        with pytest.raises(RuntimeError, match="permanently erased"):
            await storage.restore(path, content)

        # The failed restore did not resurrect the board namespace or the object.
        assert not board_root.exists()
        assert not Path(path).exists()

    asyncio.run(scenario())


def test_save_publishes_atomically_and_cleans_failed_temporary(
    tmp_path,
    monkeypatch,
) -> None:
    from okto_pulse.community.adapters import storage as storage_module

    storage = CommunityFileSystemStorage(str(tmp_path / "uploads"))

    def _replace_failure(_source, _target):
        raise OSError("simulated atomic publish failure")

    monkeypatch.setattr(storage_module.os, "replace", _replace_failure)

    async def scenario() -> None:
        with pytest.raises(OSError, match="atomic publish failure"):
            await storage.save("board-atomic", "partial.bin", b"complete-content")

    asyncio.run(scenario())
    board_root = storage.base_dir / "board-atomic"
    assert list(board_root.iterdir()) == []


def test_delete_rejects_paths_outside_storage_scope(tmp_path) -> None:
    storage = CommunityFileSystemStorage(str(tmp_path / "uploads"))
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"must-survive")

    async def scenario() -> None:
        with pytest.raises(ValueError, match="escapes storage root"):
            await storage.delete(str(outside))

    asyncio.run(scenario())
    assert outside.read_bytes() == b"must-survive"
