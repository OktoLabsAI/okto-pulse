"""Incompatible installations fail with guidance and preserve their database."""
import os
from pathlib import Path
import sqlite3
import site
import subprocess
import sys

import pytest


@pytest.mark.parametrize("command", ["init", "serve"])
@pytest.mark.parametrize("explicit", [False, True])
def test_incompatible_storage_guidance(tmp_path, command, explicit):
    home = tmp_path / "user"
    data = home / ".okto-pulse"
    db = data / "data" / "pulse.db"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE previous_data (value TEXT)")
        connection.execute("INSERT INTO previous_data VALUES ('preserve me')")
    before = db.read_bytes()
    env = os.environ.copy()
    env["PYTHONUSERBASE"] = site.getuserbase()
    for key in ("DATA_DIR", "DATABASE_URL", "KG_BASE_DIR", "UPLOAD_DIR", "METRICS_DIR"):
        env.pop(key, None)
    env.update(USERPROFILE=str(home), HOME=str(home), OKTO_PULSE_NO_BANNER="1")
    if explicit:
        env["DATA_DIR"] = str(data)
    result = subprocess.run(
        [sys.executable, "-m", "okto_pulse.community.cli", command],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert str(data) in result.stderr
    assert "earlier Pulse versions" in result.stderr
    assert "manually rename the entire data home" in result.stderr
    assert "okto-pulse init" in result.stderr
    assert "storage_format_incompatible:version" in result.stderr
    assert "Traceback" not in result.stderr
    assert db.read_bytes() == before
    assert not Path(str(db) + "-wal").exists()
    assert not Path(str(db) + "-shm").exists()
