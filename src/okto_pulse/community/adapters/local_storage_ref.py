"""Opaque reference codec for Community-local graph artifacts."""

from __future__ import annotations

import base64
from pathlib import Path

from okto_pulse.core.kg.interfaces.storage_ref import StorageRef


LOCAL_STORAGE_NAMESPACE = "community_local_graph_v1"


def local_storage_ref(path: Path) -> StorageRef:
    raw = str(Path(path).resolve()).encode("utf-8")
    token = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return StorageRef(token=token, namespace=LOCAL_STORAGE_NAMESPACE)


def resolve_local_storage_ref(ref: StorageRef) -> Path:
    if ref.namespace != LOCAL_STORAGE_NAMESPACE:
        raise ValueError("unsupported_local_storage_ref_namespace")
    padding = "=" * (-len(ref.token) % 4)
    try:
        raw = base64.urlsafe_b64decode(ref.token + padding).decode("utf-8")
    except Exception as exc:
        raise ValueError("invalid_local_storage_ref") from exc
    return Path(raw).resolve()


__all__ = [
    "LOCAL_STORAGE_NAMESPACE",
    "local_storage_ref",
    "resolve_local_storage_ref",
]
