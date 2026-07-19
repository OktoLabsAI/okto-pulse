"""Crash-consistent versioned layout for Community Global Discovery."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


ACTIVE_GENERATION_FILENAME = "active_generation.json"
GENERATIONS_DIRNAME = "discovery.generations"
GENERATION_MANIFEST_FILENAME = "generation_manifest.json"
LAYOUT_VERSION = 1
_GENERATION_ID_RE = re.compile(r"^gdr_[A-Za-z0-9_-]{4,96}$")


class GlobalDiscoveryLayoutError(RuntimeError):
    code = "global_discovery_generation_pointer_invalid"

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class ActiveGeneration:
    generation_id: str
    graph_path: Path
    manifest_sha256: str


def canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_generation_id(value: str) -> str:
    if _GENERATION_ID_RE.fullmatch(value) is None:
        raise GlobalDiscoveryLayoutError("unsafe_generation_id")
    return value


def generations_root(legacy_path: Path) -> Path:
    return legacy_path.parent / GENERATIONS_DIRNAME


def generation_dir(legacy_path: Path, generation_id: str) -> Path:
    safe_id = validate_generation_id(generation_id)
    root = generations_root(legacy_path).resolve(strict=False)
    candidate = (root / safe_id).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:  # defensive even after the strict id regex
        raise GlobalDiscoveryLayoutError("generation_outside_root") from exc
    return candidate


def generation_graph_path(legacy_path: Path, generation_id: str) -> Path:
    return generation_dir(legacy_path, generation_id) / legacy_path.name


def active_pointer_path(legacy_path: Path) -> Path:
    return legacy_path.parent / ACTIVE_GENERATION_FILENAME


def fsync_directory(path: Path) -> bool:
    flags = os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0))
    try:
        fd = os.open(str(path), flags)
    except OSError:
        return False
    try:
        os.fsync(fd)
    except OSError:
        return False
    finally:
        os.close(fd)
    return True


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        with tmp.open("x", encoding="utf-8") as stream:
            json.dump(dict(payload), stream, sort_keys=True, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        return fsync_directory(path.parent)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def write_generation_manifest(
    legacy_path: Path,
    generation_id: str,
    payload: Mapping[str, Any],
) -> tuple[str, bool]:
    safe_id = validate_generation_id(generation_id)
    binding = {
        "layout_version": LAYOUT_VERSION,
        "generation_id": safe_id,
        **dict(payload),
    }
    manifest_sha256 = canonical_sha256(binding)
    document = {**binding, "manifest_sha256": manifest_sha256}
    supported = write_json_atomic(
        generation_dir(legacy_path, safe_id) / GENERATION_MANIFEST_FILENAME,
        document,
    )
    return manifest_sha256, supported


def _load_generation_manifest(
    legacy_path: Path,
    generation_id: str,
    expected_sha256: str,
) -> dict[str, Any]:
    path = generation_dir(legacy_path, generation_id) / GENERATION_MANIFEST_FILENAME
    try:
        with path.open("r", encoding="utf-8") as stream:
            raw = json.load(stream)
    except (OSError, ValueError, TypeError) as exc:
        raise GlobalDiscoveryLayoutError("generation_manifest_unreadable") from exc
    if not isinstance(raw, dict):
        raise GlobalDiscoveryLayoutError("generation_manifest_not_object")
    supplied = str(raw.get("manifest_sha256") or "")
    binding = {key: value for key, value in raw.items() if key != "manifest_sha256"}
    computed = canonical_sha256(binding)
    if (
        supplied != computed
        or supplied != expected_sha256
        or raw.get("layout_version") != LAYOUT_VERSION
        or raw.get("generation_id") != generation_id
    ):
        raise GlobalDiscoveryLayoutError("generation_manifest_hash_mismatch")
    return raw


def read_active_generation(legacy_path: Path) -> ActiveGeneration | None:
    pointer = active_pointer_path(legacy_path)
    if not pointer.exists():
        return None
    try:
        with pointer.open("r", encoding="utf-8") as stream:
            raw = json.load(stream)
    except (OSError, ValueError, TypeError) as exc:
        raise GlobalDiscoveryLayoutError("active_pointer_unreadable") from exc
    if not isinstance(raw, dict):
        raise GlobalDiscoveryLayoutError("active_pointer_not_object")
    supplied_pointer_hash = str(raw.get("pointer_sha256") or "")
    binding = {key: value for key, value in raw.items() if key != "pointer_sha256"}
    if supplied_pointer_hash != canonical_sha256(binding):
        raise GlobalDiscoveryLayoutError("active_pointer_hash_mismatch")
    if raw.get("layout_version") != LAYOUT_VERSION:
        raise GlobalDiscoveryLayoutError("active_pointer_version_mismatch")
    generation_id = validate_generation_id(str(raw.get("generation_id") or ""))
    manifest_sha256 = str(raw.get("manifest_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", manifest_sha256):
        raise GlobalDiscoveryLayoutError("active_pointer_manifest_hash_invalid")
    _load_generation_manifest(legacy_path, generation_id, manifest_sha256)
    return ActiveGeneration(
        generation_id=generation_id,
        graph_path=generation_graph_path(legacy_path, generation_id),
        manifest_sha256=manifest_sha256,
    )


def resolve_active_graph_path(legacy_path: Path) -> Path:
    active = read_active_generation(legacy_path)
    return active.graph_path if active is not None else legacy_path


def switch_active_generation(
    legacy_path: Path,
    *,
    generation_id: str,
    manifest_sha256: str,
) -> bool:
    safe_id = validate_generation_id(generation_id)
    _load_generation_manifest(legacy_path, safe_id, manifest_sha256)
    binding = {
        "layout_version": LAYOUT_VERSION,
        "generation_id": safe_id,
        "manifest_sha256": manifest_sha256,
    }
    return write_json_atomic(
        active_pointer_path(legacy_path),
        {**binding, "pointer_sha256": canonical_sha256(binding)},
    )


def restore_legacy_generation(legacy_path: Path) -> bool:
    pointer = active_pointer_path(legacy_path)
    try:
        pointer.unlink()
    except FileNotFoundError:
        return fsync_directory(pointer.parent)
    return fsync_directory(pointer.parent)


__all__ = [
    "ACTIVE_GENERATION_FILENAME",
    "ActiveGeneration",
    "GENERATION_MANIFEST_FILENAME",
    "GENERATIONS_DIRNAME",
    "GlobalDiscoveryLayoutError",
    "active_pointer_path",
    "canonical_sha256",
    "fsync_directory",
    "generation_dir",
    "generation_graph_path",
    "read_active_generation",
    "resolve_active_graph_path",
    "restore_legacy_generation",
    "switch_active_generation",
    "validate_generation_id",
    "write_generation_manifest",
    "write_json_atomic",
]
