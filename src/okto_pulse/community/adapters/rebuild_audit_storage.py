"""Filesystem-backed rebuild/audit artifact store for Community."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from okto_pulse.core.kg.interfaces.cognitive_pending_work import (
    CognitivePendingRecordRef,
    CognitivePendingWorkProvider,
)
from okto_pulse.core.kg.interfaces.rebuild_audit_storage import (
    RebuildAuditArtifactStore,
    RebuildAuditKey,
)


def default_community_rebuild_base_dir() -> Path:
    """Resolve the local-first rebuild artifact root for Community."""

    configured = os.getenv("OKTO_PULSE_REBUILD_BASE_DIR")
    base_dir = (
        Path(configured)
        if configured
        else Path(tempfile.gettempdir()) / "okto_pulse_kg_rebuild"
    )
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir


class CommunityFileSystemRebuildAuditArtifactStore(RebuildAuditArtifactStore):
    """Preserve the current local-first rebuild/audit directory layout."""

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._lock = threading.Lock()

    def _namespace_dir(self, key: RebuildAuditKey) -> Path:
        audit_dir = self._base_dir / "rebuild" / "audit"
        generations_dir = self._base_dir / "rebuild" / "generations" / key.board_id
        if key.namespace == "event_audit":
            return audit_dir / "events" / key.board_id
        if key.namespace == "cognitive_pending":
            return audit_dir / "cognitive_pending" / key.board_id
        if key.namespace == "confirmation_audit":
            return audit_dir / "confirmation" / key.board_id
        if key.namespace == "run_audit":
            return audit_dir
        if key.namespace == "generation_current":
            return generations_dir
        if key.namespace == "generation_history":
            return generations_dir / "history"
        raise ValueError(f"unsupported rebuild audit namespace: {key.namespace}")

    def _artifact_id(self, key: RebuildAuditKey) -> str:
        if key.namespace in {"cognitive_pending", "generation_history"}:
            if not key.kg_generation_id:
                raise ValueError(f"{key.namespace} key requires kg_generation_id")
            return key.kg_generation_id
        if not key.artifact_id:
            raise ValueError(f"{key.namespace} key requires artifact_id")
        return key.artifact_id

    def _path(self, key: RebuildAuditKey) -> Path:
        return self._namespace_dir(key) / f"{self._artifact_id(key)}.json"

    def write_json_atomic(
        self,
        key: RebuildAuditKey,
        payload: Mapping[str, Any],
    ) -> None:
        with self._lock:
            self._write_json_atomic_unlocked(key, payload)

    def _write_json_atomic_unlocked(
        self,
        key: RebuildAuditKey,
        payload: Mapping[str, Any],
    ) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(dict(payload), fh, indent=2)
        tmp.replace(path)

    def read_json(self, key: RebuildAuditKey) -> dict[str, Any] | None:
        path = self._path(key)
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        return payload if isinstance(payload, dict) else None

    def exists(self, key: RebuildAuditKey) -> bool:
        return self._path(key).exists()

    def list_json(self, prefix: RebuildAuditKey) -> Sequence[dict[str, Any]]:
        directory = self._namespace_dir(prefix)
        if not directory.exists():
            return []
        if prefix.kg_generation_id or prefix.artifact_id:
            payload = self.read_json(prefix)
            return [payload] if payload is not None else []
        rows: list[dict[str, Any]] = []
        for path in sorted(directory.glob("*.json")):
            try:
                with path.open("r", encoding="utf-8") as fh:
                    payload = json.load(fh)
            except Exception:
                continue
            if isinstance(payload, dict):
                rows.append(payload)
        return rows

    def replace_json(
        self,
        key: RebuildAuditKey,
        transform: Callable[[dict[str, Any] | None], dict[str, Any]],
    ) -> dict[str, Any]:
        with self._lock:
            current = self.read_json(key)
            next_payload = transform(current)
            self._write_json_atomic_unlocked(key, next_payload)
            return dict(next_payload)


class CommunityFileSystemCognitivePendingWorkProvider(CognitivePendingWorkProvider):
    """Discover local cognitive_pending ledgers for the closeout worker."""

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir

    def list_records(self) -> Sequence[CognitivePendingRecordRef]:
        root = self._base_dir / "rebuild" / "audit" / "cognitive_pending"
        if not root.is_dir():
            return []

        records: list[CognitivePendingRecordRef] = []
        for board_dir in sorted(root.iterdir()):
            if not board_dir.is_dir():
                continue
            for record_path in sorted(board_dir.glob("*.json")):
                records.append(
                    CognitivePendingRecordRef(
                        board_id=board_dir.name,
                        kg_generation_id=record_path.stem,
                    )
                )
        return records


__all__ = [
    "CommunityFileSystemCognitivePendingWorkProvider",
    "CommunityFileSystemRebuildAuditArtifactStore",
    "default_community_rebuild_base_dir",
]
