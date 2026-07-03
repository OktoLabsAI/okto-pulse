"""Filesystem-backed rebuild/audit artifact store for Community."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from okto_pulse.core.kg.interfaces.rebuild_audit_storage import (
    RebuildAuditArtifactStore,
    RebuildAuditKey,
)


class CommunityFileSystemRebuildAuditArtifactStore(RebuildAuditArtifactStore):
    """Preserve the current local-first rebuild/audit directory layout."""

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._lock = threading.Lock()

    def _namespace_dir(self, key: RebuildAuditKey) -> Path:
        audit_dir = self._base_dir / "rebuild" / "audit"
        if key.namespace == "event_audit":
            return audit_dir / "events" / key.board_id
        if key.namespace == "cognitive_pending":
            return audit_dir / "cognitive_pending" / key.board_id
        if key.namespace == "confirmation_audit":
            return audit_dir / "confirmation" / key.board_id
        if key.namespace == "run_audit":
            return audit_dir
        raise ValueError(f"unsupported rebuild audit namespace: {key.namespace}")

    def _artifact_id(self, key: RebuildAuditKey) -> str:
        if key.namespace == "cognitive_pending":
            if not key.kg_generation_id:
                raise ValueError("cognitive_pending key requires kg_generation_id")
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


__all__ = ["CommunityFileSystemRebuildAuditArtifactStore"]
