"""Filesystem-backed rebuild/audit artifact store for Community."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
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
        if key.namespace == "source_manifest":
            return self._base_dir / "rebuild" / "manifests"
        if key.namespace == "confirmation_token":
            return self._base_dir / "rebuild" / "confirmations"
        if key.namespace == "rebuild_report":
            return self._base_dir / "rebuild" / "reports"
        if key.namespace == "candidate_decision":
            return self._base_dir / "candidate_decisions" / key.board_id
        if key.namespace == "rebaseline_audit":
            return self._base_dir / "rebuild" / "rebaseline_audit"
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

    def delete_json(self, key: RebuildAuditKey) -> bool:
        try:
            self._path(key).unlink()
            return True
        except FileNotFoundError:
            return False

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

    def quarantine_paths(
        self,
        *,
        board_id: str,
        graph_type: str,
        affected_paths: Sequence[str],
        reason: str,
        reason_bucket: str,
        correlation_ids: Sequence[str],
        kg_generation_id: str | None,
        retention_days: int,
        scope_roots: Sequence[str],
        base_dir_hint: str | None = None,
    ) -> Mapping[str, Any]:
        from okto_pulse.core.kg.quarantine import (
            MANIFEST_FILENAME,
            QUARANTINE_DIRNAME,
            QuarantineError,
            QuarantineErrorCode,
        )

        roots = [Path(root).resolve() for root in scope_roots]
        resolved_paths: list[Path] = []
        for raw in affected_paths:
            candidate = Path(raw).resolve()
            if not self._is_in_scope(candidate, roots):
                raise QuarantineError(
                    QuarantineErrorCode.AFFECTED_PATH_OUT_OF_SCOPE,
                    retryable=False,
                    reason=f"path {candidate} is not under any KG storage root",
                )
            resolved_paths.append(candidate)

        quarantine_id = f"q_{secrets.token_urlsafe(16)}"
        quarantine_root = self._quarantine_base(base_dir_hint)
        quarantine_dir = quarantine_root / QUARANTINE_DIRNAME / quarantine_id
        try:
            quarantine_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise QuarantineError(
                QuarantineErrorCode.QUARANTINE_STORAGE_UNAVAILABLE,
                retryable=True,
                reason=f"quarantine_id collision: {quarantine_id}",
            ) from exc
        except OSError as exc:
            raise QuarantineError(
                QuarantineErrorCode.QUARANTINE_STORAGE_UNAVAILABLE,
                retryable=True,
                reason=f"mkdir failed: {exc}",
            ) from exc

        moved_relatives: list[str] = []
        files_moved = 0
        try:
            for src in resolved_paths:
                if not src.exists():
                    moved_relatives.append(src.name)
                    continue
                dst = quarantine_dir / src.name
                shutil.move(str(src), str(dst))
                moved_relatives.append(src.name)
                files_moved += 1

            now = datetime.now(timezone.utc)
            manifest = {
                "quarantine_id": quarantine_id,
                "board_id": board_id,
                "graph_type": graph_type,
                "reason": reason,
                "reason_bucket": reason_bucket,
                "correlation_ids": list(correlation_ids),
                "affected_paths_relative": moved_relatives,
                "kg_generation_id": kg_generation_id,
                "software_version": self._software_version(),
                "quarantined_at": now.isoformat(),
                "retention_until": (
                    now + timedelta(days=retention_days)
                ).isoformat(),
                "files_moved": files_moved,
            }
            manifest_path = quarantine_dir / MANIFEST_FILENAME
            with manifest_path.open("w", encoding="utf-8") as fh:
                json.dump(manifest, fh, indent=2)
        except QuarantineError:
            raise
        except Exception as exc:
            partial_dir = quarantine_dir.with_name(quarantine_dir.name + ".partial")
            try:
                if partial_dir.exists():
                    partial_dir = quarantine_dir.with_name(
                        f"{quarantine_dir.name}.partial.{secrets.token_hex(4)}"
                    )
                quarantine_dir.rename(partial_dir)
            except OSError:
                pass
            raise QuarantineError(
                QuarantineErrorCode.QUARANTINE_STORAGE_UNAVAILABLE,
                retryable=True,
                reason=f"manifest write failed: {exc} (preserved at {partial_dir})",
            ) from exc

        return {
            **manifest,
            "manifest_ref": str(manifest_path),
        }

    def list_quarantine_manifests(
        self,
        *,
        active_after_iso: str | None = None,
        base_dir_hint: str | None = None,
    ) -> Sequence[Mapping[str, Any]]:
        from okto_pulse.core.kg.quarantine import MANIFEST_FILENAME, QUARANTINE_DIRNAME

        root = self._quarantine_base(base_dir_hint) / QUARANTINE_DIRNAME
        if not root.exists():
            return []
        active_after = (
            datetime.fromisoformat(active_after_iso) if active_after_iso else None
        )
        rows: list[dict[str, Any]] = []
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            manifest_path = entry / MANIFEST_FILENAME
            try:
                with manifest_path.open("r", encoding="utf-8") as fh:
                    payload = json.load(fh)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            if active_after is not None:
                try:
                    retention_until = datetime.fromisoformat(
                        str(payload["retention_until"])
                    )
                except (KeyError, ValueError):
                    continue
                if retention_until <= active_after:
                    continue
            rows.append(payload)
        return rows

    def read_quarantine_manifest(
        self,
        *,
        quarantine_id: str,
        base_dir_hint: str | None = None,
    ) -> Mapping[str, Any] | None:
        from okto_pulse.core.kg.quarantine import MANIFEST_FILENAME, QUARANTINE_DIRNAME

        manifest_path = (
            self._quarantine_base(base_dir_hint)
            / QUARANTINE_DIRNAME
            / quarantine_id
            / MANIFEST_FILENAME
        )
        try:
            with manifest_path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    def _quarantine_base(self, base_dir_hint: str | None) -> Path:
        return Path(base_dir_hint) if base_dir_hint else self._base_dir

    @staticmethod
    def _is_in_scope(path: Path, scope_roots: Sequence[Path]) -> bool:
        for root in scope_roots:
            try:
                path.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    @staticmethod
    def _software_version() -> str:
        try:
            from importlib.metadata import version

            return version("okto-pulse-core")
        except Exception:
            return "unknown"


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
