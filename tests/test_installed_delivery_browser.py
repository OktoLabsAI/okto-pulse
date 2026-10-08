"""Opt-in full installed SPA over disposable native delivery data."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
import urllib.request
from types import SimpleNamespace

import pytest

from test_global_discovery_recovery_installed_e2e import (
    _free_port, _isolated_runtime_environment, _prove_installed_pair,
    _run_checked, _stop_server,
)

@pytest.mark.e2e
def test_installed_spa_preserves_native_partial_delivery(tmp_path):
    required = ("PULSE_NATIVE_INSTALLED_PYTHON", "PULSE_NATIVE_WHEEL_DIR", "PULSE_NATIVE_DELIVERY_FIXTURE")
    if not all(os.environ.get(key) for key in required):
        pytest.skip("Set installed Python, wheel pair and native delivery fixture paths")
    python = Path(os.environ[required[0]]).resolve()
    wheels = Path(os.environ[required[1]]).resolve()
    source = Path(os.environ[required[2]]).resolve()
    assert (source / "native-context.json").is_file()
    assert (source / "partial-proof/receipt.key").is_file()
    data = tmp_path / "runtime"
    (data / "data").mkdir(parents=True)
    env = _isolated_runtime_environment(data)
    env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
               KG_EMBEDDING_MODE="stub", KG_EMBEDDING_DIM="384",
               OKTO_PULSE_SKIP_DEMO_SEED="1", OKTO_PULSE_TERMS_ACCEPTED="1",
               OKTO_PULSE_NO_BANNER="1", OKTO_PULSE_METRICS_BEACON_STARTUP_DELAY_SECONDS="600",
               PYTHONUNBUFFERED="1", NO_PROXY="127.0.0.1,localhost")
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PUBLIC_HOST", "PUBLIC_API_PORT", "PUBLIC_MCP_PORT"):
        env.pop(key, None)
    _prove_installed_pair(root=tmp_path, python=python,
        core_wheel=next(wheels.glob("okto_pulse_core-*.whl")),
        community_wheel=next(wheels.glob("okto_pulse-*.whl")), env=env)
    # Same-format native test snapshot, not import/conversion of old storage.
    database = data / "data/pulse.db"
    with sqlite3.connect((source / "candidates.sqlite").as_uri() + "?mode=ro", uri=True) as original:
        with sqlite3.connect(database) as copied:
            original.backup(copied)
            # Local UI identity owns the disposable Board; proof authors untouched.
            copied.execute("UPDATE boards SET owner_id='local-user' WHERE id='board'")
            previous_records = dict(copied.execute("SELECT id, payload FROM card_delivery_evidence_records").fetchall())
    shutil.copytree(source / "partial-proof", data / "evidence")
    api_port, mcp_port = _free_port(), _free_port()
    assert api_port != mcp_port
    env.update(OKTO_PULSE_PORT=str(api_port), OKTO_PULSE_MCP_PORT=str(mcp_port))
    cli = [str(python), "-I", "-m", "okto_pulse.community.cli"]
    initialized = _run_checked(cli + ["init"],
                               cwd=tmp_path, env=env, timeout=240)
    (tmp_path / "init.log").write_text(initialized.stdout + initialized.stderr, encoding="utf-8")
    log = tmp_path / "server.log"
    with log.open("x", encoding="utf-8") as stream:
        process = subprocess.Popen(cli + ["serve", "--api-port", str(api_port), "--mcp-port", str(mcp_port), "--accept-terms"],
            cwd=tmp_path, env=env, stdout=stream, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        server = SimpleNamespace(process=process)
        try:
            deadline = time.monotonic() + 180
            while True:
                assert process.poll() is None, log.read_text(encoding="utf-8")[-8000:]
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{api_port}/health", timeout=2) as response:
                        if response.status == 200:
                            break
                except OSError:
                    pass
                assert time.monotonic() < deadline, log.read_text(encoding="utf-8")[-8000:]
                time.sleep(0.25)
            frontend = Path(__file__).resolve().parents[1] / "frontend"
            browser_env = dict(os.environ, CI="1", PULSE_INSTALLED_DELIVERY_TEST="1", E2E_BASE_URL=f"http://127.0.0.1:{api_port}")
            result = subprocess.run(
                ["node", "node_modules/@playwright/test/cli.js", "test",
                 "--config=playwright.installed-delivery.config.ts", "--output=" + str(tmp_path / "browser")],
                cwd=frontend, env=browser_env, capture_output=True, timeout=120)
            (tmp_path / "browser.log").write_bytes(result.stdout + result.stderr)
            assert result.returncode == 0, (result.stdout + result.stderr).decode("utf-8", errors="replace")
            with sqlite3.connect(database) as persisted:
                rows = persisted.execute("SELECT id,payload,actor_id,actor_kind,kind,card_id FROM card_delivery_evidence_records").fetchall()
            assert {row[0]: row[1] for row in rows if row[0] in previous_records} == previous_records
            added = [row for row in rows if row[0] not in previous_records]
            assert len(added) == 1
            assert added[0][2:] == ("local-user", "human", "test", "test-card")
            (tmp_path / "installed-browser-result.json").write_text(json.dumps({
                "installed_python": str(python), "api_port": api_port,
                "database": str(database), "browser_returncode": result.returncode,
                "prior_records_preserved": len(previous_records), "new_records": len(added),
                "recorded_id": added[0][0], "recorded_actor": added[0][2],
                "embedding": "deterministic stub; graph semantics not under test",
            }, indent=2), encoding="utf-8")
        finally:
            _stop_server(server)
