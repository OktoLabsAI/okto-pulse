"""The distributed SPA must be qualified before Community creates runtime state."""

import json
import hashlib
from pathlib import Path
import shutil
import subprocess

import pytest


def test_windows_checkout_preserves_packaged_asset_bytes(tmp_path):
    """Git must not change the bytes authenticated by the SPA manifest."""
    from okto_pulse.community.adapters.frontend_distribution import require_compatible_frontend

    repo = Path(__file__).resolve().parents[1]
    relative = Path("src/okto_pulse/community/frontend_dist")
    bundle = tmp_path / relative
    bundle.parent.mkdir(parents=True)
    shutil.copytree(repo / relative, bundle)
    shutil.copyfile(repo / ".gitattributes", tmp_path / ".gitattributes")

    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True,
                       capture_output=True)

    git("init", "--quiet")
    git("config", "core.autocrlf", "true")
    git("config", "core.safecrlf", "false")
    git("add", ".gitattributes", relative.as_posix())
    # Fresh checkout from the index, with the Windows conversion enabled.
    output = tmp_path / "checkout"
    output.mkdir()
    git("checkout-index", "--all", "--force", f"--prefix={output.as_posix()}/")
    require_compatible_frontend(output / relative)


def test_tracked_frontend_bytes_match_the_distribution_manifest(tmp_path):
    """Changing attributes must also migrate previously normalized Git blobs."""
    from okto_pulse.community.adapters.frontend_distribution import require_compatible_frontend

    repo = Path(__file__).resolve().parents[1]
    relative = Path("src/okto_pulse/community/frontend_dist")
    manifest_name = "pulse-frontend-contract.json"
    manifest = json.loads((repo / relative / manifest_name).read_text(encoding="utf-8"))
    names = [manifest_name, *(entry["path"] for entry in manifest["files"])]
    subprocess.run(
        ["git", "-C", str(repo), "checkout-index", "--stdin", "-z",
         f"--prefix={tmp_path.as_posix()}/"],
        input=b"".join((relative / name).as_posix().encode("utf-8") + b"\0" for name in names),
        check=True, capture_output=True,
    )
    require_compatible_frontend(tmp_path / relative)


def test_incompatible_frontend_refuses_before_runtime_state(tmp_path, monkeypatch):
    from okto_pulse.community import main

    (tmp_path / "index.html").write_text("<html>old frontend</html>", encoding="utf-8")
    (tmp_path / "pulse-frontend-contract.json").write_text(json.dumps({
        "format": "pulse-frontend-distribution/v1", "release_version": "0.0.0",
        "core_contract": "pulse-edition-api/4", "files": [],
    }), encoding="utf-8")
    monkeypatch.setattr(main, "FRONTEND_DIR", tmp_path)
    monkeypatch.setattr(main, "_ensure_data_dir", lambda _: pytest.fail("incompatible frontend reached runtime mutation"))
    with pytest.raises(RuntimeError, match="frontend_"):
        main.create_community_app()


def _bundle(root):
    from okto_pulse.community import __version__
    from okto_pulse.community.adapters.distribution_compatibility import EXPECTED_CORE_CONTRACT

    content = b"<html>qualified frontend</html>"
    (root / "index.html").write_bytes(content)
    manifest = {"format": "pulse-frontend-distribution/v1", "release_version": __version__,
                "core_contract": EXPECTED_CORE_CONTRACT,
                "files": [{"path": "index.html", "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}]}
    _write(root, manifest)
    return manifest


def _write(root, manifest):
    (root / "pulse-frontend-contract.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_valid_frontend_is_read_only_and_packaged_bundle_is_admitted(tmp_path):
    from okto_pulse.community.main import FRONTEND_DIR
    from okto_pulse.community.adapters.frontend_distribution import require_compatible_frontend

    _bundle(tmp_path)
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    require_compatible_frontend(tmp_path)
    require_compatible_frontend(tmp_path)
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before
    require_compatible_frontend(FRONTEND_DIR)


@pytest.mark.parametrize("damage,reason", [
    ("release", "frontend_release_mismatch"),
    ("core", "frontend_core_contract_mismatch"),
    ("same_size_content", "frontend_assets_mismatch"),
    ("missing_asset", "frontend_assets_mismatch"),
    ("extra_asset", "frontend_assets_mismatch"),
    ("missing_manifest", "frontend_contract_missing"),
    ("traversal", "frontend_contract_invalid"),
    ("duplicate", "frontend_contract_invalid"),
    ("boolean_size", "frontend_contract_invalid"),
    ("oversized_manifest", "frontend_contract_invalid"),
    ("oversized_asset", "frontend_asset_budget_exceeded"),
])
def test_incompatible_artifacts_are_refused_without_repair(tmp_path, damage, reason):
    from okto_pulse.community.adapters.frontend_distribution import require_compatible_frontend

    manifest = _bundle(tmp_path)
    if damage == "release":
        manifest["release_version"] = "0.0.0"
    elif damage == "core":
        manifest["core_contract"] = "other"
    elif damage == "same_size_content":
        content = (tmp_path / "index.html").read_bytes()
        (tmp_path / "index.html").write_bytes(b"x" + content[1:])
    elif damage == "missing_asset":
        (tmp_path / "index.html").unlink()
    elif damage == "extra_asset":
        (tmp_path / "stale.js").write_text("stale")
    elif damage == "traversal":
        manifest["files"][0]["path"] = "../foreign.html"
    elif damage == "duplicate":
        manifest["files"].append(dict(manifest["files"][0]))
    elif damage == "boolean_size":
        manifest["files"][0]["size"] = True
    elif damage == "oversized_asset":
        manifest["files"][0]["size"] = 64 * 1024 * 1024 + 1
    _write(tmp_path, manifest)
    if damage == "missing_manifest":
        (tmp_path / "pulse-frontend-contract.json").unlink()
    elif damage == "oversized_manifest":
        (tmp_path / "pulse-frontend-contract.json").write_bytes(b" " * (256 * 1024 + 1))
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    with pytest.raises(RuntimeError, match=reason):
        require_compatible_frontend(tmp_path)
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before
