"""Offline reset cleanup: only board IDs owned by the SQLite being reset.

Unknown directories, Global Discovery and quarantine/backup roots are not garbage
collected. A complete path preflight precedes the first destructive operation.
"""

from contextlib import closing
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat


def _plain_path(path: Path) -> None:
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"reset refuses a symlink/reparse path: {part}")


def _plain_tree(path: Path) -> None:
    _plain_path(path)
    if not path.is_dir():
        raise ValueError(f"reset expected a board directory: {path}")
    for root, directories, files in os.walk(
        path, followlinks=False, onerror=_raise_walk_error
    ):
        for name in (*directories, *files):
            _plain_path(Path(root) / name)


def _raise_walk_error(error: OSError) -> None:
    raise error


@dataclass(frozen=True)
class BoardResetPlan:
    boards_root: Path
    targets: tuple[tuple[Path, int, int], ...]

    def apply(self) -> None:
        # Recheck the entire target set before deleting any of it.
        for path, device, inode in self.targets:
            _plain_tree(path)
            info = path.stat()
            if path.resolve().parent != self.boards_root.resolve() or (
                info.st_dev,
                info.st_ino,
            ) != (device, inode):
                raise ValueError(
                    f"reset board directory changed after preflight: {path}"
                )
        for path, _device, _inode in self.targets:
            shutil.rmtree(path)
            print(f"  Cleared owned board graph: {path}")


def plan_board_reset(settings, db_path: Path) -> BoardResetPlan:
    """Resolve ownership before SQLite deletion; refuse ambiguous/corrupt input."""
    if getattr(settings, "database_url", None):
        from sqlalchemy.engine import make_url

        url = make_url(settings.database_url)
        if (
            not url.drivername.startswith("sqlite")
            or not url.database
            or Path(url.database).expanduser().absolute().resolve() != db_path.resolve()
        ):
            raise ValueError(
                "reset requires the default data-dir SQLite; custom relational databases are not reset"
            )
    root = (
        Path(getattr(settings, "kg_base_dir", None) or settings.data_dir)
        .expanduser()
        .absolute()
    )
    boards = root / "boards"
    _plain_path(boards)
    if not boards.exists():
        return BoardResetPlan(boards, ())
    if not boards.is_dir():
        raise ValueError(f"reset expected a boards directory: {boards}")
    if not db_path.is_file():
        # Without the source catalog no per-board ownership can be inferred.
        raise ValueError(
            "reset cannot identify owned graphs without the SQLite board catalog"
        )
    _plain_path(db_path)
    with closing(
        sqlite3.connect(db_path.absolute().as_uri() + "?mode=ro", uri=True)
    ) as conn:
        ids = tuple(row[0] for row in conn.execute("SELECT id FROM boards"))
    targets = []
    for board_id in ids:
        if not isinstance(board_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]+", board_id
        ):
            raise ValueError("reset found an unsafe board ID in the SQLite catalog")
    for board_id in sorted(set(ids)):
        path = boards / board_id
        _plain_path(path)
        if not path.exists():
            continue
        _plain_tree(path)
        if path.resolve().parent != boards.resolve():
            raise ValueError("reset target escaped the configured boards directory")
        info = path.stat()
        targets.append((path, info.st_dev, info.st_ino))
    return BoardResetPlan(boards, tuple(targets))
