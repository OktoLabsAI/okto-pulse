"""Read-only admission of the packaged SPA before Community runtime startup."""

import hashlib
import json
from pathlib import Path, PurePosixPath

from okto_pulse.community import __version__
from okto_pulse.community.adapters.distribution_compatibility import (
    EXPECTED_CORE_CONTRACT, IncompatiblePulseDistribution,
)
from okto_pulse.community.adapters.filesystem_erasure import is_filesystem_alias

_CONTRACT_FILE = "pulse-frontend-contract.json"
_MAX_MANIFEST_BYTES = 256 * 1024
_MAX_ASSET_BYTES = 64 * 1024 * 1024
_MAX_FILES = 1000


def _refuse(reason):
    raise IncompatiblePulseDistribution(reason)


def require_compatible_frontend(directory: Path) -> None:
    """No regeneration, installation, state-directory creation or schema work."""
    try:
        _verify(Path(directory))
    except IncompatiblePulseDistribution:
        raise
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise IncompatiblePulseDistribution("frontend_contract_invalid") from exc


def _verify(root: Path) -> None:
    if is_filesystem_alias(root) or not root.is_dir():
        _refuse("frontend_contract_missing")
    manifest_path = root / _CONTRACT_FILE
    if is_filesystem_alias(manifest_path) or not manifest_path.is_file():
        _refuse("frontend_contract_missing")
    with manifest_path.open("rb") as stream:
        encoded = stream.read(_MAX_MANIFEST_BYTES + 1)
    if len(encoded) > _MAX_MANIFEST_BYTES:
        _refuse("frontend_contract_invalid")
    manifest = json.loads(encoded)
    if type(manifest) is not dict or set(manifest) != {"format", "release_version", "core_contract", "files"}:
        _refuse("frontend_contract_invalid")
    if manifest["format"] != "pulse-frontend-distribution/v1":
        _refuse("frontend_contract_invalid")
    if manifest["release_version"] != __version__:
        _refuse("frontend_release_mismatch")
    if manifest["core_contract"] != EXPECTED_CORE_CONTRACT:
        _refuse("frontend_core_contract_mismatch")
    files = manifest["files"]
    if type(files) is not list or not 1 <= len(files) <= _MAX_FILES:
        _refuse("frontend_contract_invalid")
    expected = {}
    total = 0
    for item in files:
        if type(item) is not dict or set(item) != {"path", "size", "sha256"}:
            _refuse("frontend_contract_invalid")
        name, size, digest = item["path"], item["size"], item["sha256"]
        if (type(name) is not str or not name or "\\" in name or ":" in name
                or PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts
                or str(PurePosixPath(name)) != name or name == _CONTRACT_FILE
                or name.casefold() in {key.casefold() for key in expected}):
            _refuse("frontend_contract_invalid")
        if type(size) is not int or size < 0 or type(digest) is not str or len(digest) != 64:
            _refuse("frontend_contract_invalid")
        if any(character not in "0123456789abcdef" for character in digest):
            _refuse("frontend_contract_invalid")
        total += size
        if total > _MAX_ASSET_BYTES:
            _refuse("frontend_asset_budget_exceeded")
        expected[name] = item
    if "index.html" not in expected:
        _refuse("frontend_contract_invalid")

    actual, pending, entries = set(), [root], 0
    while pending:
        for path in pending.pop().iterdir():
            entries += 1
            if entries > 2 * _MAX_FILES or is_filesystem_alias(path):
                _refuse("frontend_assets_mismatch")
            if path.is_dir():
                pending.append(path)
            elif path.is_file():
                name = path.relative_to(root).as_posix()
                if name != _CONTRACT_FILE:
                    actual.add(name)
            else:
                _refuse("frontend_assets_mismatch")
    if actual != set(expected):
        _refuse("frontend_assets_mismatch")
    for name, item in expected.items():
        path = root / name
        if path.stat().st_size != item["size"]:
            _refuse("frontend_assets_mismatch")
        digest, size = hashlib.sha256(), 0
        with path.open("rb") as stream:
            while chunk := stream.read(65536):
                size += len(chunk)
                if size > item["size"]:
                    _refuse("frontend_assets_mismatch")
                digest.update(chunk)
        if size != item["size"] or digest.hexdigest() != item["sha256"]:
            _refuse("frontend_assets_mismatch")
