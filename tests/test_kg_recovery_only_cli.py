from __future__ import annotations

import ast
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import inspect
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib
from types import SimpleNamespace
from typing import Mapping

import pytest

from okto_pulse.community import kg_recovery_only as recovery


BOARD_ID = "15877207-c147-4805-96d7-d53a625571df"
INSTALL_HASH = "9" * 64
EXECUTOR_HASH = "3" * 64
ENTRYPOINT_HASH = "4" * 64
SCHEMA_HASH = "5" * 64
SOURCE_SCOPE = {
    "data/pulse.db": "1" * 64,
    f"boards/{BOARD_ID}/graph.lbug": "2" * 64,
}
SOURCE_STORAGE = {"pulse.db": "1" * 64}
EXECUTION_CONTRACT = {
    "operation": "rebuild",
    "reason": "isolated regression rehearsal",
    "offline_ports": [8100, 8101],
    "admission_timeout_seconds": 180.0,
    "run_timeout_seconds": 3600.0,
    "poll_seconds": 0.1,
    "batch_size": 5,
}


def _terminal(source_home: Path) -> dict[str, object]:
    board_storage_post_teardown = {"graph.lbug": "a" * 64}
    return {
        "run_id": "run_0123456789abcdef01234567",
        "manifest_ref": "manifest_rehearsal",
        "source_set_hash": "6" * 64,
        "current_kg_generation_id": "generation_rehearsal",
        "report_ref": "report://rehearsal",
        "report_id": "report_rehearsal",
        "publishable_status": "completed",
        "promotion_outcome": "promoted",
        "event_emitted": True,
        "orphan_count": 0,
        "quarantine_id": "q_rehearsal",
        "report_source_hash": "7" * 64,
        "graph_schema_version": "1",
        "sqlite_storage_before": SOURCE_STORAGE,
        "sqlite_storage_after": {"pulse.db": "8" * 64},
        "board_storage_post_teardown": board_storage_post_teardown,
        "board_storage_post_teardown_sha256": recovery._canonical_json_hash(
            board_storage_post_teardown
        ),
        "rehearsal_source_unchanged": True,
        "rehearsal_source_home": str(source_home),
    }


def _stub_attestation_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recovery, "_hash_executor_file", lambda: EXECUTOR_HASH)
    monkeypatch.setattr(
        recovery,
        "_hash_recovery_entrypoint_metadata",
        lambda: ENTRYPOINT_HASH,
    )
    monkeypatch.setattr(
        recovery,
        "_rehearsal_scope_snapshot",
        lambda _home, _board: dict(SOURCE_SCOPE),
    )
    monkeypatch.setattr(
        recovery,
        "_sqlite_schema_fingerprint",
        lambda _db: SCHEMA_HASH,
    )
    monkeypatch.setattr(
        recovery,
        "_sqlite_storage_fingerprints",
        lambda _db: dict(SOURCE_STORAGE),
    )


def _build_attestation(
    *,
    source_home: Path,
    receipt_path: Path,
    issued_at: datetime | None = None,
) -> dict[str, object]:
    return recovery._build_rehearsal_attestation(
        board_id=BOARD_ID,
        source_home=source_home,
        source_snapshot=SOURCE_SCOPE,
        source_schema=SCHEMA_HASH,
        source_storage=SOURCE_STORAGE,
        terminal=_terminal(source_home),
        install_fingerprint=INSTALL_HASH,
        receipt_path=receipt_path,
        execution_contract=EXECUTION_CONTRACT,
        issued_at=issued_at,
    )


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--data-home", "C:/pulse", "--board-id", BOARD_ID],
        [
            "--data-home",
            "C:/pulse",
            "--board-id",
            BOARD_ID,
            "--execute",
            "--expected-install-fingerprint",
            INSTALL_HASH,
        ],
        [
            "--data-home",
            "C:/copy",
            "--board-id",
            BOARD_ID,
            "--rehearsal-copy-of",
            "C:/live",
            "--expected-install-fingerprint",
            INSTALL_HASH,
        ],
        [
            "--data-home",
            "C:/pulse",
            "--board-id",
            BOARD_ID,
            "--inspect-install",
            "--rehearsal-receipt",
            "C:/receipt.json",
        ],
    ],
)
def test_parser_refuses_implicit_or_unattested_modes(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        recovery._parse_args(argv)


def test_inspect_install_does_not_touch_data_home_or_execute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    supplied_home = tmp_path / "must-not-be-created"
    evidence = recovery.InstallEvidence(
        fingerprint=INSTALL_HASH,
        distributions=(),
        runtime=(("python_version", "test"),),
    )
    monkeypatch.setattr(recovery, "_install_evidence", lambda **_kwargs: evidence)
    monkeypatch.setattr(recovery, "_hash_executor_file", lambda: EXECUTOR_HASH)
    monkeypatch.setattr(
        recovery,
        "_hash_recovery_entrypoint_metadata",
        lambda: ENTRYPOINT_HASH,
    )
    monkeypatch.setattr(
        recovery,
        "_execute",
        lambda *_args, **_kwargs: pytest.fail("inspect invoked recovery"),
    )

    exit_code = recovery.main(
        [
            "--data-home",
            str(supplied_home.resolve()),
            "--board-id",
            BOARD_ID,
            "--inspect-install",
        ]
    )

    assert exit_code == 0
    assert not supplied_home.exists()
    payload = json.loads(capsys.readouterr().out)
    assert payload["event"] == "installed_pulse_evidence"
    assert payload["install_fingerprint"] == INSTALL_HASH
    assert payload["executor_sha256"] == EXECUTOR_HASH
    assert payload["entrypoints_sha256"] == ENTRYPOINT_HASH


def test_process_oracle_detects_real_launchers_but_not_ancestor_shells() -> None:
    assert recovery._is_pulse_runtime_process(
        "python.exe",
        r"python.exe C:\Python313\Scripts\okto-pulse.exe serve",
    )
    assert recovery._is_pulse_runtime_process(
        "okto-pulse-kg-recovery-only.exe",
        "okto-pulse-kg-recovery-only.exe --data-home D:/copy --inspect-install",
    )
    assert recovery._is_pulse_runtime_process(
        "python.exe",
        "python -m okto_pulse.community.kg_recovery_only --inspect-install",
    )
    assert recovery._is_pulse_runtime_process(
        "okto-pulse-kg-recovery-only.exe",
        "",
    )
    assert recovery._is_pulse_runtime_process("okto-pulse.exe", "")
    assert not recovery._is_pulse_runtime_process(
        "powershell.exe",
        "powershell -Command python okto-pulse-kg-recovery-only.py",
    )
    assert not recovery._is_pulse_runtime_process(
        "cmd.exe",
        "cmd /c okto-pulse.exe serve",
    )
    assert not recovery._is_pulse_runtime_process("python.exe", "python worker.py")


def test_windows_process_oracle_denies_dedicated_names_without_command_line() -> None:
    rows = (
        {
            "ProcessId": 1,
            "ParentProcessId": 0,
            "CreationDate": "2026-08-15T10:00:00+00:00",
            "Name": "python.exe",
            "ExecutablePath": str(Path(sys.executable).resolve()),
            "CommandLine": "python harmless.py",
        },
        {
            "ProcessId": 2,
            "ParentProcessId": 0,
            "CreationDate": "2026-08-15T10:00:00+00:00",
            "Name": "okto-pulse.exe",
            "ExecutablePath": None,
            "CommandLine": None,
        },
        {
            "ProcessId": 3,
            "ParentProcessId": 0,
            "CreationDate": "2026-08-15T10:00:00+00:00",
            "Name": "okto-pulse-kg-recovery-only.exe",
            "ExecutablePath": None,
            "CommandLine": "",
        },
    )

    assert recovery._windows_pulse_process_candidates(
        rows,
        current_pid=1,
        launcher_paths=frozenset(),
    ) == (
        {"pid": 2, "name": "okto-pulse.exe"},
        {"pid": 3, "name": "okto-pulse-kg-recovery-only.exe"},
    )


def test_windows_process_oracle_excludes_only_exact_direct_self_launcher(
    tmp_path: Path,
) -> None:
    launcher = tmp_path / "okto-pulse-kg-recovery-only.exe"
    launcher.write_bytes(b"test launcher identity")
    rows = (
        {
            "ProcessId": 200,
            "ParentProcessId": 100,
            "CreationDate": "2026-08-15T10:00:00.300000+00:00",
            "Name": "python.exe",
            "ExecutablePath": str(Path(sys.executable).resolve()),
            "CommandLine": "python child-runtime.py",
        },
        {
            "ProcessId": 100,
            "ParentProcessId": 50,
            "CreationDate": "2026-08-15T10:00:00.100000+00:00",
            "Name": launcher.name,
            "ExecutablePath": str(launcher.resolve()),
            "CommandLine": f'"{launcher.resolve()}" --data-home D:/copy',
        },
        {
            "ProcessId": 300,
            "ParentProcessId": 50,
            "CreationDate": "2026-08-15T10:00:00.200000+00:00",
            "Name": launcher.name,
            "ExecutablePath": str(launcher.resolve()),
            "CommandLine": f'"{launcher.resolve()}" --data-home D:/other-copy',
        },
        {
            "ProcessId": 400,
            "ParentProcessId": 50,
            "CreationDate": "2026-08-15T10:00:00.200000+00:00",
            "Name": "python.exe",
            "ExecutablePath": str(Path(sys.executable).resolve()),
            "CommandLine": (r"python.exe C:\Python313\Scripts\okto-pulse.exe serve"),
        },
    )

    candidates = recovery._windows_pulse_process_candidates(
        rows,
        current_pid=200,
        launcher_paths=frozenset({launcher.resolve()}),
    )

    assert candidates == (
        {"pid": 300, "name": launcher.name},
        {"pid": 400, "name": "python.exe"},
    )


def test_windows_process_oracle_allows_exact_distlib_launcher_chain(
    tmp_path: Path,
) -> None:
    launcher = tmp_path / "venv" / "Scripts" / "okto-pulse-kg-recovery-only.exe"
    launcher.parent.mkdir(parents=True)
    launcher.write_bytes(b"installed launcher")
    shim = launcher.parent / "python.exe"
    shim.write_bytes(b"venv interpreter shim")
    base_python = tmp_path / "base-python.exe"
    base_python.write_bytes(b"base interpreter")
    command_line = f'"{shim.resolve()}" "{launcher.resolve()}" --execute'
    rows = (
        {
            "ProcessId": 300,
            "ParentProcessId": 200,
            "CreationDate": "2026-08-15T10:00:00.300000+00:00",
            "Name": "python.exe",
            "ExecutablePath": str(base_python.resolve()),
            "CommandLine": command_line,
        },
        {
            "ProcessId": 200,
            "ParentProcessId": 100,
            "CreationDate": "2026-08-15T10:00:00.200000+00:00",
            "Name": "python.exe",
            "ExecutablePath": str(shim.resolve()),
            "CommandLine": command_line,
        },
        {
            "ProcessId": 100,
            "ParentProcessId": 50,
            "CreationDate": "2026-08-15T10:00:00.100000+00:00",
            "Name": launcher.name,
            "ExecutablePath": str(launcher.resolve()),
            "CommandLine": f'"{launcher.resolve()}" --execute',
        },
    )

    assert (
        recovery._windows_pulse_process_candidates(
            rows,
            current_pid=300,
            launcher_paths=frozenset({launcher.resolve()}),
        )
        == ()
    )


@pytest.mark.parametrize(
    "parent_kind",
    ("different_launcher", "python_module", "pulse_serve"),
)
def test_windows_process_oracle_never_exempts_unbound_recovery_parent(
    tmp_path: Path,
    parent_kind: str,
) -> None:
    installed_launcher = tmp_path / "installed" / "okto-pulse-kg-recovery-only.exe"
    installed_launcher.parent.mkdir()
    installed_launcher.write_bytes(b"installed launcher")
    different_launcher = tmp_path / "other" / "okto-pulse-kg-recovery-only.exe"
    different_launcher.parent.mkdir()
    different_launcher.write_bytes(b"other launcher")
    if parent_kind == "different_launcher":
        parent = {
            "ProcessId": 100,
            "ParentProcessId": 50,
            "CreationDate": "2026-08-15T10:00:00.100000+00:00",
            "Name": different_launcher.name,
            "ExecutablePath": str(different_launcher.resolve()),
            "CommandLine": f'"{different_launcher.resolve()}" --execute',
        }
    elif parent_kind == "python_module":
        parent = {
            "ProcessId": 100,
            "ParentProcessId": 50,
            "CreationDate": "2026-08-15T10:00:00.100000+00:00",
            "Name": "python.exe",
            "ExecutablePath": str(Path(sys.executable).resolve()),
            "CommandLine": "python -m okto_pulse.community.kg_recovery_only",
        }
    else:
        parent = {
            "ProcessId": 100,
            "ParentProcessId": 50,
            "CreationDate": "2026-08-15T10:00:00.100000+00:00",
            "Name": "python.exe",
            "ExecutablePath": str(Path(sys.executable).resolve()),
            "CommandLine": (r"python.exe C:\Python313\Scripts\okto-pulse.exe serve"),
        }
    rows = (
        {
            "ProcessId": 200,
            "ParentProcessId": 100,
            "CreationDate": "2026-08-15T10:00:00.200000+00:00",
            "Name": "python.exe",
            "ExecutablePath": str(Path(sys.executable).resolve()),
            "CommandLine": "python child-runtime.py",
        },
        parent,
    )

    candidates = recovery._windows_pulse_process_candidates(
        rows,
        current_pid=200,
        launcher_paths=frozenset({installed_launcher.resolve()}),
    )

    assert candidates == ({"pid": 100, "name": parent["Name"]},)


def test_windows_process_oracle_refuses_reused_parent_pid_created_after_child(
    tmp_path: Path,
) -> None:
    launcher = tmp_path / "okto-pulse-kg-recovery-only.exe"
    launcher.write_bytes(b"installed launcher")
    rows = (
        {
            "ProcessId": 200,
            "ParentProcessId": 100,
            "CreationDate": "2026-08-15T10:00:00.100000+00:00",
            "Name": "python.exe",
            "ExecutablePath": str(Path(sys.executable).resolve()),
            "CommandLine": "python child-runtime.py",
        },
        {
            "ProcessId": 100,
            "ParentProcessId": 50,
            # A process created after the child cannot be its true parent;
            # this models Win32 PID reuse in a stale ParentProcessId field.
            "CreationDate": "2026-08-15T10:00:01.000000+00:00",
            "Name": launcher.name,
            "ExecutablePath": str(launcher.resolve()),
            "CommandLine": f'"{launcher.resolve()}" --execute',
        },
    )

    assert recovery._windows_pulse_process_candidates(
        rows,
        current_pid=200,
        launcher_paths=frozenset({launcher.resolve()}),
    ) == ({"pid": 100, "name": launcher.name},)


@pytest.mark.parametrize("outer_kind", ("pulse_serve", "outer_recovery"))
def test_windows_process_oracle_does_not_hide_pulse_above_exact_launcher_chain(
    tmp_path: Path,
    outer_kind: str,
) -> None:
    launcher = tmp_path / "venv" / "Scripts" / "okto-pulse-kg-recovery-only.exe"
    launcher.parent.mkdir(parents=True)
    launcher.write_bytes(b"installed launcher")
    shim = launcher.parent / "python.exe"
    shim.write_bytes(b"venv interpreter shim")
    base_python = tmp_path / "base-python.exe"
    base_python.write_bytes(b"base interpreter")
    command_line = f'"{shim.resolve()}" "{launcher.resolve()}" --execute'
    if outer_kind == "pulse_serve":
        outer = {
            "ProcessId": 50,
            "ParentProcessId": 10,
            "CreationDate": "2026-08-15T10:00:00.010000+00:00",
            "Name": "python.exe",
            "ExecutablePath": str(Path(sys.executable).resolve()),
            "CommandLine": (r"python.exe C:\Python313\Scripts\okto-pulse.exe serve"),
        }
    else:
        outer = {
            "ProcessId": 50,
            "ParentProcessId": 10,
            "CreationDate": "2026-08-15T10:00:00.010000+00:00",
            "Name": "python.exe",
            "ExecutablePath": str(Path(sys.executable).resolve()),
            "CommandLine": "python -m okto_pulse.community.kg_recovery_only",
        }
    rows = (
        {
            "ProcessId": 300,
            "ParentProcessId": 200,
            "CreationDate": "/Date(1786788000300)/",
            "Name": "python.exe",
            "ExecutablePath": str(base_python.resolve()),
            "CommandLine": command_line,
        },
        {
            "ProcessId": 200,
            "ParentProcessId": 100,
            "CreationDate": "/Date(1786788000200)/",
            "Name": "python.exe",
            "ExecutablePath": str(shim.resolve()),
            "CommandLine": command_line,
        },
        {
            "ProcessId": 100,
            "ParentProcessId": 50,
            "CreationDate": "/Date(1786788000100)/",
            "Name": launcher.name,
            "ExecutablePath": str(launcher.resolve()),
            "CommandLine": f'"{launcher.resolve()}" --execute',
        },
        outer,
    )

    candidates = recovery._windows_pulse_process_candidates(
        rows,
        current_pid=300,
        launcher_paths=frozenset({launcher.resolve()}),
    )

    assert candidates == ({"pid": 50, "name": outer["Name"]},)


@pytest.mark.parametrize(
    "aliased_relative",
    (
        Path("data/pulse.db"),
        Path("boards") / BOARD_ID / "graph.lbug",
    ),
)
def test_rehearsal_copy_refuses_hardlinked_critical_file_before_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    aliased_relative: Path,
) -> None:
    source_home = tmp_path / "live"
    target_home = tmp_path / "copy"
    critical = (
        Path("data/pulse.db"),
        Path("boards") / BOARD_ID / "graph.lbug",
    )
    for relative in critical:
        source_path = source_home / relative
        target_path = target_home / relative
        source_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(f"critical:{relative.as_posix()}".encode())
        shutil.copyfile(source_path, target_path)

    aliased_source = source_home / aliased_relative
    aliased_target = target_home / aliased_relative
    aliased_target.unlink()
    try:
        os.link(aliased_source, aliased_target)
    except OSError as exc:
        pytest.skip(f"hardlinks unavailable on test filesystem: {exc}")

    monkeypatch.setattr(
        recovery,
        "_sqlite_schema_fingerprint",
        lambda _path: pytest.fail("schema probe ran before hardlink refusal"),
    )
    with pytest.raises(recovery.RecoveryRefused, match="rehearsal_copy_alias_refused"):
        recovery._assert_rehearsal_copy(
            source_home=source_home,
            target_home=target_home,
            board_id=BOARD_ID,
        )


def test_module_has_no_online_or_global_recovery_startup_calls() -> None:
    source_path = Path(recovery.__file__).resolve()
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    called_names: set[str] = set()
    called_attributes: set[str] = set()
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                called_names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                called_attributes.add(node.func.attr)
        elif isinstance(node, ast.ImportFrom):
            imported_names.update(alias.name for alias in node.names)

    assert "init_db" not in imported_names | called_names
    assert "recover_stale_claims" not in called_attributes
    assert "lifespan_context" not in called_attributes
    assert "start_scheduler" not in called_attributes
    assert "start_all" not in called_attributes
    assert "create_community_app" in called_names


def _create_app_settings_db(path: Path, ddl: str) -> None:
    import sqlite3

    with sqlite3.connect(path) as connection:
        connection.execute(ddl)


def test_required_recovery_schema_accepts_canonical_app_settings_key_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "pulse.db"
    _create_app_settings_db(
        db_path,
        'CREATE TABLE app_settings ("key" VARCHAR(64) NOT NULL, '
        'value VARCHAR(64) NOT NULL, PRIMARY KEY ("key"))',
    )
    monkeypatch.setattr(
        recovery,
        "REQUIRED_RECOVERY_COLUMNS",
        {"app_settings": frozenset({"key", "value"})},
    )

    recovery._assert_required_recovery_schema(db_path)


def test_app_settings_precomposition_pin_matches_installed_orm_model() -> None:
    from okto_pulse.community.adapters.sqlalchemy_models import AppSetting

    actual = tuple(
        (
            str(column.name),
            str(column.type).upper(),
            int(not column.nullable),
            None if column.server_default is None else str(column.server_default.arg),
            int(column.primary_key),
        )
        for column in AppSetting.__table__.columns
    )

    assert actual == recovery.EXPECTED_APP_SETTINGS_TABLE_INFO


@pytest.mark.parametrize(
    ("ddl", "error"),
    (
        (
            "CREATE TABLE app_settings (id TEXT PRIMARY KEY, value VARCHAR(64) NOT NULL)",
            "recovery_schema_columns_missing.*app_settings.*key",
        ),
        (
            'CREATE TABLE app_settings ("key" VARCHAR(64) NOT NULL PRIMARY KEY)',
            "recovery_schema_columns_missing.*app_settings.*value",
        ),
        (
            'CREATE TABLE app_settings ("key" VARCHAR(64) NOT NULL, '
            "value VARCHAR(64) NOT NULL)",
            "recovery_app_settings_schema_mismatch",
        ),
        (
            'CREATE TABLE app_settings ("key" VARCHAR(64) NOT NULL, '
            'value VARCHAR(64), PRIMARY KEY ("key"))',
            "recovery_app_settings_schema_mismatch",
        ),
        (
            'CREATE TABLE app_settings ("key" TEXT NOT NULL, '
            'value VARCHAR(64) NOT NULL, PRIMARY KEY ("key"))',
            "recovery_app_settings_schema_mismatch",
        ),
        (
            'CREATE TABLE app_settings ("key" VARCHAR(64) NOT NULL, '
            'value VARCHAR(64) NOT NULL, id TEXT, PRIMARY KEY ("key"))',
            "recovery_app_settings_schema_mismatch",
        ),
    ),
)
def test_required_recovery_schema_refuses_app_settings_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ddl: str,
    error: str,
) -> None:
    db_path = tmp_path / "pulse.db"
    _create_app_settings_db(db_path, ddl)
    monkeypatch.setattr(
        recovery,
        "REQUIRED_RECOVERY_COLUMNS",
        {"app_settings": frozenset({"key", "value"})},
    )

    with pytest.raises(recovery.RecoveryRefused, match=error):
        recovery._assert_required_recovery_schema(db_path)


def test_preexisting_rebuild_temp_is_refused_before_plan_or_artifact_read(
    tmp_path: Path,
) -> None:
    rebuild_root = tmp_path / "rebuild"
    transient = rebuild_root / "audit" / ".active.json.deadbeef.tmp"
    transient.parent.mkdir(parents=True)
    transient.write_bytes(b"forensic crash cut")
    baseline = recovery._snapshot_tree_hashes(rebuild_root)

    with pytest.raises(
        recovery.RecoveryRefused,
        match="preexisting_rebuild_transient_refused",
    ):
        recovery._assert_no_rebuild_transients(baseline)

    assert transient.read_bytes() == b"forensic crash cut"
    source = inspect.getsource(recovery._execute_under_serve_lock)
    snapshot_offset = source.index("rebuild_baseline = _snapshot_tree_hashes")
    refusal_offset = source.index("_assert_no_rebuild_transients(rebuild_baseline)")
    graph_snapshot_offset = source.index(
        "board_storage_baseline = _snapshot_tree_hashes"
    )
    composition_offset = source.index("app = create_community_app()")
    health_offset = source.index("raw_health = _offline_cold_graph_health")
    plan_offset = source.index("plan = _select_recovery_run_plan")
    assert (
        snapshot_offset
        < refusal_offset
        < graph_snapshot_offset
        < composition_offset
        < health_offset
        < plan_offset
    )
    assert "raw_health = await _real_health" not in source


def _make_dangling_directory_alias(path: Path) -> None:
    missing_target = path.parent / f"{path.name}-missing-target"
    try:
        path.symlink_to(missing_target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    assert not path.exists()
    assert path.is_symlink() or recovery._is_filesystem_alias(path)


@pytest.mark.asyncio
async def test_dangling_rebuild_root_is_refused_before_composition_or_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.community import main as community_main

    data_home = tmp_path / "copy"
    data_home.mkdir()
    rebuild_root = data_home / "rebuild"
    _make_dangling_directory_alias(rebuild_root)
    raw_link = os.readlink(rebuild_root)
    create_calls: list[bool] = []

    def forbidden_create_app():  # noqa: ANN202
        create_calls.append(True)
        pytest.fail("composition created before dangling rebuild root refusal")

    monkeypatch.setattr(community_main, "create_community_app", forbidden_create_app)

    with pytest.raises(
        recovery.RecoveryRefused,
        match="recovery_artifact_alias_refused",
    ):
        await recovery._execute_under_serve_lock(
            SimpleNamespace(board_id=BOARD_ID),
            data_home=data_home,
            db_path=data_home / "data" / "pulse.db",
            owner_id="owner-1",
            schema_fingerprint="schema-test",
            serve_lock=object(),
            heartbeat=SimpleNamespace(failure=None),
            require_fresh=False,
        )

    assert create_calls == []
    assert os.readlink(rebuild_root) == raw_link
    assert not rebuild_root.exists()


@pytest.mark.parametrize(
    "relative_root",
    (
        Path("quarantine"),
        Path("boards") / BOARD_ID,
    ),
)
def test_snapshot_refuses_dangling_quarantine_and_board_root_aliases(
    tmp_path: Path,
    relative_root: Path,
) -> None:
    root = tmp_path / relative_root
    root.parent.mkdir(parents=True, exist_ok=True)
    _make_dangling_directory_alias(root)

    with pytest.raises(
        recovery.RecoveryRefused,
        match="recovery_artifact_alias_refused",
    ):
        recovery._snapshot_tree_hashes(root)


def test_snapshot_refuses_regular_file_root_and_dangling_parent_alias(
    tmp_path: Path,
) -> None:
    regular_file = tmp_path / "quarantine"
    regular_file.write_bytes(b"not a governed tree")
    with pytest.raises(
        recovery.RecoveryRefused,
        match="recovery_artifact_root_not_directory",
    ):
        recovery._snapshot_tree_hashes(regular_file)

    boards_parent = tmp_path / "boards"
    _make_dangling_directory_alias(boards_parent)
    with pytest.raises(
        recovery.RecoveryRefused,
        match="recovery_artifact_alias_refused",
    ):
        recovery._snapshot_tree_hashes(boards_parent / BOARD_ID)


def test_quarantine_ids_refuses_dangling_root_alias(tmp_path: Path) -> None:
    quarantine_root = tmp_path / "quarantine"
    _make_dangling_directory_alias(quarantine_root)

    with pytest.raises(
        recovery.RecoveryRefused,
        match="quarantine_root_alias_refused",
    ):
        recovery._quarantine_ids(quarantine_root)


def test_snapshot_refuses_unverifiable_root_lstat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "rebuild"
    root.mkdir()
    original_lstat = Path.lstat

    def guarded_lstat(path: Path):  # noqa: ANN202
        if path == root:
            raise PermissionError("simulated lstat denial")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", guarded_lstat)
    with pytest.raises(
        recovery.RecoveryRefused,
        match="recovery_artifact_alias_refused_unverifiable",
    ):
        recovery._snapshot_tree_hashes(root)


def test_snapshot_read_denial_reports_only_safe_relative_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "boards" / BOARD_ID
    root.mkdir(parents=True)
    graph = root / "graph.lbug"
    graph.write_bytes(b"native graph bytes")
    original_open = recovery._open_snapshot_file_exclusive

    @contextmanager
    def guarded_open(path: Path):  # noqa: ANN202
        if path == graph:
            error = PermissionError(13, "simulated Ladybug sharing violation")
            error.winerror = 32
            raise error
        with original_open(path) as handle:
            yield handle

    monkeypatch.setattr(recovery, "_open_snapshot_file_exclusive", guarded_open)
    with pytest.raises(recovery.RecoveryRefused) as captured:
        recovery._snapshot_tree_hashes(root)

    message = str(captured.value)
    assert "recovery_artifact_read_failed" in message
    assert "relative='graph.lbug'" in message
    assert "type=regular" in message
    assert "attrs=" in message
    assert "errno=13" in message
    assert "winerror=32" in message
    assert str(tmp_path) not in message


def test_snapshot_enumeration_denial_is_typed_and_path_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "boards" / BOARD_ID
    root.mkdir(parents=True)
    original_scandir = os.scandir

    def guarded_scandir(path):  # noqa: ANN001, ANN202
        if Path(path) == root:
            error = PermissionError(13, "simulated enumeration denial")
            error.winerror = 5
            raise error
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", guarded_scandir)
    with pytest.raises(recovery.RecoveryRefused) as captured:
        recovery._snapshot_tree_hashes(root)

    message = str(captured.value)
    assert "recovery_artifact_enumeration_failed" in message
    assert "relative_dir='.'" in message
    assert "errno=13" in message
    assert "winerror=5" in message
    assert str(tmp_path) not in message


def test_snapshot_refuses_file_inserted_between_structural_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "rebuild"
    root.mkdir()
    original = root / "a.json"
    original.write_bytes(b"stable baseline")
    late = root / "late.tmp"
    real_open = recovery._open_snapshot_file_exclusive
    inserted = False

    @contextmanager
    def inserting_open(path: Path):  # noqa: ANN202
        nonlocal inserted
        if path == original and not inserted:
            late.write_bytes(b"crash evidence inserted during snapshot")
            inserted = True
        with real_open(path) as handle:
            yield handle

    monkeypatch.setattr(recovery, "_open_snapshot_file_exclusive", inserting_open)

    with pytest.raises(
        recovery.RecoveryRefused,
        match="recovery_artifact_tree_changed_while_hashing",
    ):
        recovery._snapshot_tree_hashes(root)

    assert late.read_bytes() == b"crash evidence inserted during snapshot"


def test_snapshot_refuses_nested_file_inserted_during_second_hash_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "rebuild"
    nested = root / "audit"
    nested.mkdir(parents=True)
    original = nested / "a.json"
    original.write_bytes(b"stable baseline")
    late = nested / "late.tmp"
    real_open = recovery._open_snapshot_file_exclusive
    open_count = 0

    @contextmanager
    def inserting_open(path: Path):  # noqa: ANN202
        nonlocal open_count
        if path == original:
            open_count += 1
            if open_count == 2:
                late.write_bytes(b"inserted after second-pass enumeration")
        with real_open(path) as handle:
            yield handle

    monkeypatch.setattr(recovery, "_open_snapshot_file_exclusive", inserting_open)

    with pytest.raises(
        recovery.RecoveryRefused,
        match="recovery_artifact_tree_changed_while_hashing",
    ):
        recovery._snapshot_tree_hashes(root)

    assert open_count == 2
    assert late.read_bytes() == b"inserted after second-pass enumeration"


def test_snapshot_nested_tree_uses_authoritative_lstat_identity(tmp_path: Path) -> None:
    root = tmp_path / "rebuild"
    nested = root / "audit" / "board"
    nested.mkdir(parents=True)
    artifact = nested / "a.json"
    artifact.write_bytes(b"nested artifact")

    assert recovery._snapshot_tree_hashes(root) == {
        "audit/board/a.json": hashlib.sha256(b"nested artifact").hexdigest()
    }


def test_offline_cold_health_is_conservative_and_snapshot_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.community.adapters import kuzu_graph_path_resolver

    root = tmp_path / "boards" / BOARD_ID
    root.mkdir(parents=True)
    graph = root / "graph.lbug"
    graph.write_bytes(b"closed graph")
    state = SimpleNamespace(
        path=graph,
        exists=True,
        locked=False,
        quarantined=False,
        sidecars=(),
    )
    monkeypatch.setattr(
        kuzu_graph_path_resolver.CommunityKuzuGraphPathResolver,
        "storage_state",
        lambda _self, _board_id: state,
    )
    bundle = SimpleNamespace(
        generation_repository=SimpleNamespace(
            get_current=lambda _board_id: "generation-current"
        )
    )

    health = recovery._offline_cold_graph_health(
        bundle,
        board_id=BOARD_ID,
        board_storage_root=root,
        board_storage_snapshot={"graph.lbug": "a" * 64},
    )

    assert health == {
        "graph_state": "recovery_needed",
        "metric_status": "unavailable",
        "current_kg_generation_id": "generation-current",
        "graph_storage_exists": True,
        "graph_storage_locked": False,
    }

    state.sidecars = ("graph.lbug.wal",)
    with pytest.raises(
        recovery.RecoveryRefused,
        match="cold_graph_sidecar_snapshot_drift",
    ):
        recovery._offline_cold_graph_health(
            bundle,
            board_id=BOARD_ID,
            board_storage_root=root,
            board_storage_snapshot={"graph.lbug": "a" * 64},
        )


def test_post_teardown_board_snapshot_requires_one_closed_graph(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph_hash = "a" * 64
    monkeypatch.setattr(
        recovery,
        "_snapshot_tree_hashes",
        lambda _root: {"graph.lbug": graph_hash},
    )

    snapshot, digest = recovery._capture_post_teardown_board_storage(
        data_home=tmp_path,
        board_id=BOARD_ID,
    )

    assert snapshot == {"graph.lbug": graph_hash}
    assert digest == recovery._canonical_json_hash(snapshot)

    monkeypatch.setattr(
        recovery,
        "_snapshot_tree_hashes",
        lambda _root: {
            "graph.lbug": graph_hash,
            "graph.lbug.wal": "b" * 64,
        },
    )
    with pytest.raises(
        recovery.RecoveryRefused,
        match="post_teardown_board_storage_invalid",
    ):
        recovery._capture_post_teardown_board_storage(
            data_home=tmp_path,
            board_id=BOARD_ID,
        )


def test_closed_board_snapshot_refuses_drain_timeout_before_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.community.adapters import kg_runtime

    @contextmanager
    def blocked_window(*_args, **_kwargs):  # noqa: ANN202
        raise TimeoutError("simulated graph reader did not drain")
        yield

    monkeypatch.setattr(kg_runtime, "board_storage_mutation_window", blocked_window)
    monkeypatch.setattr(
        recovery,
        "_snapshot_tree_hashes",
        lambda _root: pytest.fail("hashed before reader drain completed"),
    )

    with pytest.raises(recovery.RecoveryRefused) as captured:
        recovery._snapshot_closed_board_storage(
            board_id=BOARD_ID,
            board_storage_root=tmp_path / "boards" / BOARD_ID,
            phase="unit-timeout",
            drain_timeout_seconds=0.01,
        )

    assert "board_graph_close_before_snapshot_failed" in str(captured.value)
    assert "phase='unit-timeout'" in str(captured.value)
    assert "type=TimeoutError" in str(captured.value)


@pytest.mark.skipif(os.name != "nt", reason="Windows Ladybug handle contract")
def test_real_ladybug_close_releases_hash_handle_and_allows_explicit_reopen(
    tmp_path: Path,
) -> None:
    import textwrap

    source_root = Path(recovery.__file__).resolve().parents[2]
    child_home = tmp_path / "native-child"
    script = textwrap.dedent(
        """
        import asyncio
        from pathlib import Path
        import sys
        from okto_pulse.community import kg_recovery_only as recovery

        async def main():
            home = Path(sys.argv[1]).resolve()
            for relative in ('data', 'rebuild', 'quarantine'):
                (home / relative).mkdir(parents=True, exist_ok=True)
            recovery._configure_explicit_environment(home)
            from okto_pulse.community.main import create_community_app
            from okto_pulse.community.adapters.kuzu_graph_store import (
                CommunityKuzuGraphStore,
            )
            from okto_pulse.core.application.kg_runtime_access import (
                resolve_graph_lifecycle,
            )
            from okto_pulse.core.composition import runtime_composition_scope

            app = create_community_app()
            composition = app.state.runtime_composition
            transaction = app.state.mcp_cold_start_transaction
            board_id = '11111111-1111-4111-8111-111111111111'
            try:
                with runtime_composition_scope(composition):
                    lifecycle = resolve_graph_lifecycle()
                    store = CommunityKuzuGraphStore()
                    assert (await lifecycle.open(board_id)).opened
                    assert store.get_schema_version(board_id)
                    from okto_pulse.community.adapters.kg_runtime import (
                        BoardConnection,
                    )
                    held_reader = BoardConnection(board_id)
                    try:
                        try:
                            recovery._snapshot_closed_board_storage(
                                board_id=board_id,
                                board_storage_root=home / 'boards' / board_id,
                                phase='native-proof-held-reader',
                                drain_timeout_seconds=0.05,
                            )
                        except recovery.RecoveryRefused as exc:
                            assert 'board_graph_close_before_snapshot_failed' in str(exc)
                        else:
                            raise AssertionError('active native reader was not refused')
                    finally:
                        held_reader.close()
                    first = recovery._snapshot_closed_board_storage(
                        board_id=board_id,
                        board_storage_root=home / 'boards' / board_id,
                        phase='native-proof-first'
                    )
                    assert set(first) == {'graph.lbug'}
                    assert (await lifecycle.open(board_id)).opened
                    assert store.get_schema_version(board_id)
                    second = recovery._snapshot_closed_board_storage(
                        board_id=board_id,
                        board_storage_root=home / 'boards' / board_id,
                        phase='native-proof-second'
                    )
                    assert set(second) == {'graph.lbug'}
            finally:
                with runtime_composition_scope(composition):
                    await recovery._shutdown_composed_runtime(composition, None)
                transaction.rollback()
            post_teardown = recovery._snapshot_tree_hashes(
                home / 'boards' / board_id
            )
            assert set(post_teardown) == {'graph.lbug'}
            print('native_close_hash_reopen_teardown_ok')

        asyncio.run(main())
        """
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(source_root)
    completed = subprocess.run(
        [sys.executable, "-c", script, str(child_home)],
        cwd=source_root.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "native_close_hash_reopen_teardown_ok" in completed.stdout


def test_windows_reparse_flag_is_classified_as_filesystem_alias() -> None:
    stat_module = __import__("stat")
    reparse_flag = int(getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x00000400))
    details = SimpleNamespace(
        st_mode=stat_module.S_IFDIR,
        st_file_attributes=reparse_flag,
    )

    assert recovery._stat_is_filesystem_alias(details)


def _receipt(*, state: str | None = "authorized") -> dict[str, object]:
    from okto_pulse.core.kg.rebuild_service import rebuild_operation_run_id

    receipt = {
        "schema_version": "kg_rebuild_confirmation_receipt.v1",
        "board_id": BOARD_ID,
        "actor_id": "owner-1",
        "operation": "rebuild",
        "preflight_hash": "a" * 64,
        "manifest_ref": "manifest_authorized",
        "source_set_hash": "b" * 64,
        "confirmation_ref": f"conf_fp_{'c' * 64}",
        "user_reason": "resume exact operation",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    receipt["run_id"] = rebuild_operation_run_id(
        board_id=BOARD_ID,
        operation=str(receipt["operation"]),
        preflight_hash=str(receipt["preflight_hash"]),
        source_set_hash=str(receipt["source_set_hash"]),
        manifest_ref=str(receipt["manifest_ref"]),
    )
    if state is not None:
        receipt["receipt_state"] = state
    return receipt


def _terminal_audit(
    receipt: dict[str, object],
    *,
    outcome: str,
    reason: str,
    frozen: bool = False,
    resumable: bool = False,
) -> dict[str, object]:
    audit = {
        "run_id": receipt["run_id"],
        "board_id": receipt["board_id"],
        "actor_id": receipt["actor_id"],
        "operation": receipt["operation"],
        "manifest_ref": receipt["manifest_ref"],
        "confirmation_ref": receipt["confirmation_ref"],
        "user_reason": receipt["user_reason"],
        "outcome": outcome,
        "reason": reason,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "same_run_resume_allowed": resumable,
    }
    if frozen:
        audit.update(
            {
                "report_ref": "report://terminal",
                "report_id": "report_terminal",
                "publishable_status": "completed",
                "promotion_outcome": "promoted",
                "current_kg_generation_id": "generation_terminal",
                "event_emitted": True,
                "operator_action": None,
            }
        )
    return audit


class _ReceiptArtifactStore:
    def __init__(
        self,
        audit: dict[str, object] | None = None,
        *,
        active_rows: tuple[dict[str, object], ...] = (),
        history_rows: tuple[dict[str, object], ...] = (),
    ) -> None:
        self.audit = audit
        self.active_rows = active_rows
        self.history_rows = history_rows

    def read_json(self, key):  # noqa: ANN001, ANN201
        if key.namespace == "run_audit":
            return self.audit
        return None

    def list_json_bounded(self, key, **_kwargs):  # noqa: ANN001, ANN201
        if key.namespace == "run_audit":
            return (self.audit,) if self.audit is not None else ()
        if key.namespace != "rebuild_confirmation_receipt":
            return ()
        if key.artifact_id == "active":
            return self.active_rows
        return self.history_rows


@pytest.mark.parametrize(
    ("receipt_state", "audit_kind", "expects_resume"),
    (
        (None, "authorized", True),
        ("authorized", "resumable", True),
        ("authorized", "frozen", True),
        ("terminal", "frozen", False),
        ("terminal", "closed", False),
    ),
)
def test_receipt_discovery_delegates_shape_and_classifies_operation_state(
    monkeypatch: pytest.MonkeyPatch,
    receipt_state: str | None,
    audit_kind: str,
    expects_resume: bool,
) -> None:
    from okto_pulse.core.kg import rebuild_service

    receipt = _receipt(state=receipt_state)
    if audit_kind == "authorized":
        audit = None
    elif audit_kind == "resumable":
        audit = _terminal_audit(
            receipt,
            outcome="rebuild_failed",
            reason="lease_lost",
            resumable=True,
        )
    elif audit_kind == "frozen":
        audit = _terminal_audit(
            receipt,
            outcome="completed",
            reason="ok",
            frozen=True,
        )
    else:
        audit = _terminal_audit(
            receipt,
            outcome="failed",
            reason="lifecycle_failed",
        )
    monkeypatch.setattr(
        rebuild_service,
        "load_verified_rebuild_confirmation_receipt",
        lambda **_kwargs: dict(receipt),
    )
    bundle = SimpleNamespace(
        artifact_store=_ReceiptArtifactStore(audit),
        generation_repository=SimpleNamespace(
            get_current=lambda _board_id: "generation_terminal"
        ),
    )

    selected = recovery._discover_incomplete_receipt(
        bundle,
        board_id=BOARD_ID,
        actor_id="owner-1",
    )

    assert (selected is not None) is expects_resume


def test_receipt_discovery_handles_absent_corrupt_and_binding_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.core.kg import rebuild_service

    # Legacy artifacts outside the new confirmation-receipt namespace do not
    # make a clean first-use board ambiguous.
    bundle = SimpleNamespace(
        artifact_store=_ReceiptArtifactStore(audit={"legacy_run": True}),
        generation_repository=SimpleNamespace(get_current=lambda _board_id: None),
    )
    assert (
        recovery._discover_incomplete_receipt(
            bundle,
            board_id=BOARD_ID,
            actor_id="owner-1",
        )
        is None
    )

    # By contrast, active missing with any history in the new namespace is a
    # torn purge. The shared Core loader owns that fail-closed distinction.
    history_only_bundle = SimpleNamespace(
        artifact_store=_ReceiptArtifactStore(
            history_rows=({"malformed_history_is_still_evidence": True},),
        ),
        generation_repository=SimpleNamespace(get_current=lambda _board_id: None),
    )
    with pytest.raises(recovery.RecoveryRefused, match="integrity_invalid"):
        recovery._discover_incomplete_receipt(
            history_only_bundle,
            board_id=BOARD_ID,
            actor_id="owner-1",
        )

    def corrupt(**_kwargs):  # noqa: ANN202
        raise rebuild_service.RebuildConfirmationReceiptIntegrityError("corrupt")

    monkeypatch.setattr(
        rebuild_service,
        "load_verified_rebuild_confirmation_receipt",
        corrupt,
    )
    with pytest.raises(recovery.RecoveryRefused, match="integrity_invalid"):
        recovery._discover_incomplete_receipt(
            bundle,
            board_id=BOARD_ID,
            actor_id="owner-1",
        )

    mismatched = _receipt()
    mismatched["actor_id"] = "other-owner"
    monkeypatch.setattr(
        rebuild_service,
        "load_verified_rebuild_confirmation_receipt",
        lambda **_kwargs: mismatched,
    )
    with pytest.raises(recovery.RecoveryRefused, match="actor_mismatch"):
        recovery._discover_incomplete_receipt(
            bundle,
            board_id=BOARD_ID,
            actor_id="owner-1",
        )


@pytest.mark.parametrize("history_kind", ("missing", "conflicting"))
def test_terminal_receipt_without_exact_history_never_opens_fresh(
    history_kind: str,
) -> None:
    receipt = _receipt(state="terminal")
    audit = _terminal_audit(
        receipt,
        outcome="completed",
        reason="ok",
        frozen=True,
    )
    history_rows: tuple[dict[str, object], ...]
    if history_kind == "missing":
        history_rows = ()
    else:
        history_rows = ({**receipt, "manifest_ref": "manifest_conflict"},)
    bundle = SimpleNamespace(
        artifact_store=_ReceiptArtifactStore(
            audit,
            active_rows=(receipt,),
            history_rows=history_rows,
        ),
        generation_repository=SimpleNamespace(
            get_current=lambda _board_id: "generation_terminal"
        ),
    )

    with pytest.raises(recovery.RecoveryRefused, match="integrity_invalid"):
        recovery._discover_incomplete_receipt(
            bundle,
            board_id=BOARD_ID,
            actor_id="owner-1",
        )


def test_terminal_receipt_with_exact_history_is_fresh_rotatable() -> None:
    receipt = _receipt(state="terminal")
    audit = _terminal_audit(
        receipt,
        outcome="completed",
        reason="ok",
        frozen=True,
    )
    bundle = SimpleNamespace(
        artifact_store=_ReceiptArtifactStore(
            audit,
            active_rows=(receipt,),
            history_rows=(receipt,),
        ),
        generation_repository=SimpleNamespace(
            get_current=lambda _board_id: "generation_terminal"
        ),
    )

    assert (
        recovery._discover_incomplete_receipt(
            bundle,
            board_id=BOARD_ID,
            actor_id="owner-1",
        )
        is None
    )


def test_authorized_receipt_with_terminal_history_resumes_only_closed_audit() -> None:
    receipt = _receipt(state="authorized")
    terminal_history = {**receipt, "receipt_state": "terminal"}
    audit = _terminal_audit(
        receipt,
        outcome="completed",
        reason="ok",
        frozen=True,
    )
    bundle = SimpleNamespace(
        artifact_store=_ReceiptArtifactStore(
            audit,
            active_rows=(receipt,),
            history_rows=(terminal_history,),
        ),
        generation_repository=SimpleNamespace(
            get_current=lambda _board_id: "generation_terminal"
        ),
    )

    assert (
        recovery._discover_incomplete_receipt(
            bundle,
            board_id=BOARD_ID,
            actor_id="owner-1",
        )
        == receipt
    )


@pytest.mark.parametrize("audit_kind", ("missing", "resumable", "forged"))
def test_authorized_terminal_history_without_exact_closed_audit_is_refused(
    audit_kind: str,
) -> None:
    receipt = _receipt(state="authorized")
    terminal_history = {**receipt, "receipt_state": "terminal"}
    if audit_kind == "missing":
        audit = None
    elif audit_kind == "resumable":
        audit = _terminal_audit(
            receipt,
            outcome="rebuild_failed",
            reason="lease_lost",
            resumable=True,
        )
    else:
        audit = {
            **_terminal_audit(
                receipt,
                outcome="completed",
                reason="ok",
                frozen=True,
            ),
            "manifest_ref": "manifest_forged",
        }
    bundle = SimpleNamespace(
        artifact_store=_ReceiptArtifactStore(
            audit,
            active_rows=(receipt,),
            history_rows=(terminal_history,),
        ),
        generation_repository=SimpleNamespace(
            get_current=lambda _board_id: "generation_terminal"
        ),
    )

    with pytest.raises(recovery.RecoveryRefused, match="integrity_invalid"):
        recovery._discover_incomplete_receipt(
            bundle,
            board_id=BOARD_ID,
            actor_id="owner-1",
        )


@pytest.mark.parametrize(
    "failure_kind",
    ("missing", "corrupt", "drift"),
)
def test_resume_manifest_verification_delegates_and_selects_compensation(
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    from okto_pulse.core.kg.rebuild_sources import (
        RebuildSourceManifestIntegrityError,
        RebuildSourceManifestNotFoundError,
        SourceSetRevalidation,
    )

    receipt = _receipt()
    monkeypatch.setattr(
        recovery,
        "_discover_incomplete_receipt",
        lambda *_args, **_kwargs: receipt,
    )
    source_set = SimpleNamespace(cognitive_durable_digest={"digest": "bound"})

    class ManifestStore:
        def load_verified(self, manifest_ref, **kwargs):  # noqa: ANN001, ANN201
            assert manifest_ref == receipt["manifest_ref"]
            assert kwargs == {
                "expected_board_id": BOARD_ID,
                "expected_preflight_hash": receipt["preflight_hash"],
                "cognitive_digest": source_set.cognitive_durable_digest,
            }
            if failure_kind == "missing":
                raise RebuildSourceManifestNotFoundError("missing")
            if failure_kind == "corrupt":
                raise RebuildSourceManifestIntegrityError("corrupt")
            return SimpleNamespace(
                manifest_ref=receipt["manifest_ref"],
                source_set_hash=receipt["source_set_hash"],
            )

        def classify_revalidation(self, **_kwargs):  # noqa: ANN201
            return SimpleNamespace(outcome=SourceSetRevalidation.MANIFEST_DRIFT)

    bundle = SimpleNamespace(
        artifact_store=_ReceiptArtifactStore(),
        source_enumerator=SimpleNamespace(
            enumerate=lambda **_kwargs: source_set,
        ),
        manifest_store=ManifestStore(),
    )

    plan = recovery._select_recovery_run_plan(
        bundle,
        board_id=BOARD_ID,
        actor_id="owner-1",
        raw_health={},
    )

    assert plan.mode == "resume_compensation"
    assert plan.manifest.manifest_ref == receipt["manifest_ref"]
    assert plan.manifest.source_set_hash == receipt["source_set_hash"]
    assert plan.manifest_verification_failure


def test_resume_manifest_verified_path_remains_authorized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.core.kg.rebuild_sources import SourceSetRevalidation

    receipt = _receipt()
    monkeypatch.setattr(
        recovery,
        "_discover_incomplete_receipt",
        lambda *_args, **_kwargs: receipt,
    )
    source_set = SimpleNamespace(cognitive_durable_digest={})
    manifest = SimpleNamespace(
        manifest_ref=receipt["manifest_ref"],
        source_set_hash=receipt["source_set_hash"],
    )
    store = SimpleNamespace(
        load_verified=lambda *_args, **_kwargs: manifest,
        classify_revalidation=lambda **_kwargs: SimpleNamespace(
            outcome=SourceSetRevalidation.EQUIVALENT
        ),
    )
    bundle = SimpleNamespace(
        artifact_store=_ReceiptArtifactStore(),
        source_enumerator=SimpleNamespace(enumerate=lambda **_kwargs: source_set),
        manifest_store=store,
    )

    plan = recovery._select_recovery_run_plan(
        bundle,
        board_id=BOARD_ID,
        actor_id="owner-1",
        raw_health={},
    )

    assert plan.mode == "resume"
    assert plan.manifest is manifest
    assert plan.source_set is source_set
    assert not plan.frozen_terminal


def test_resume_rebaseline_plan_captures_pure_evidence_without_mutating_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.core.kg.rebuild_sources import SourceSetRevalidation

    receipt = _receipt()
    monkeypatch.setattr(
        recovery,
        "_discover_incomplete_receipt",
        lambda *_args, **_kwargs: receipt,
    )
    source_set = SimpleNamespace(cognitive_durable_digest={})
    manifest = SimpleNamespace(
        manifest_ref=receipt["manifest_ref"],
        source_set_hash=receipt["source_set_hash"],
    )
    evidence = {
        "outcome": "rebaseline",
        "from_source_set_hash": receipt["source_set_hash"],
        "to_source_set_hash": "d" * 64,
        "rebaselined_source_refs": ["spec:source-1"],
    }

    class ManifestStore:
        def load_verified(self, *_args, **_kwargs):  # noqa: ANN201
            return manifest

        def classify_revalidation(self, **_kwargs):  # noqa: ANN201
            return SimpleNamespace(
                outcome=SourceSetRevalidation.REBASELINE,
                to_dict=lambda: dict(evidence),
            )

        def revalidate(self, **_kwargs):  # noqa: ANN201
            pytest.fail("mutating revalidation was called during plan selection")

    plan = recovery._select_recovery_run_plan(
        SimpleNamespace(
            artifact_store=_ReceiptArtifactStore(),
            source_enumerator=SimpleNamespace(enumerate=lambda **_kwargs: source_set),
            manifest_store=ManifestStore(),
        ),
        board_id=BOARD_ID,
        actor_id="owner-1",
        raw_health={},
    )

    assert plan.mode == "resume"
    assert plan.rebaseline_evidence == evidence
    assert ".revalidate(" not in inspect.getsource(recovery)


def test_active_authorized_frozen_audit_selects_zero_effect_archive_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.core.kg.rebuild_sources import SourceSetRevalidation

    receipt = _receipt(state="authorized")
    audit = _terminal_audit(
        receipt,
        outcome="completed",
        reason="ok",
        frozen=True,
    )
    monkeypatch.setattr(
        recovery,
        "_discover_incomplete_receipt",
        lambda *_args, **_kwargs: receipt,
    )
    source_set = SimpleNamespace(cognitive_durable_digest={})
    manifest = SimpleNamespace(
        manifest_ref=receipt["manifest_ref"],
        source_set_hash=receipt["source_set_hash"],
    )
    bundle = SimpleNamespace(
        artifact_store=_ReceiptArtifactStore(audit),
        source_enumerator=SimpleNamespace(enumerate=lambda **_kwargs: source_set),
        manifest_store=SimpleNamespace(
            load_verified=lambda *_args, **_kwargs: manifest,
            classify_revalidation=lambda **_kwargs: SimpleNamespace(
                outcome=SourceSetRevalidation.EQUIVALENT
            ),
        ),
    )

    plan = recovery._select_recovery_run_plan(
        bundle,
        board_id=BOARD_ID,
        actor_id="owner-1",
        raw_health={},
    )

    assert plan.mode == "resume"
    assert plan.frozen_terminal
    assert plan.receipt is receipt


@pytest.mark.parametrize("audit_kind", ("frozen", "resumable_effect"))
def test_terminal_evidence_manifest_failure_never_selects_compensation(
    monkeypatch: pytest.MonkeyPatch,
    audit_kind: str,
) -> None:
    from okto_pulse.core.kg.rebuild_sources import (
        RebuildSourceManifestNotFoundError,
    )

    receipt = _receipt(state="authorized")
    if audit_kind == "frozen":
        audit = _terminal_audit(
            receipt,
            outcome="completed",
            reason="ok",
            frozen=True,
        )
    else:
        audit = _terminal_audit(
            receipt,
            outcome="rebuild_failed",
            reason="lease_lost",
            resumable=True,
        )
        audit["report_ref"] = "report://partial-terminal-effect"
    monkeypatch.setattr(
        recovery,
        "_discover_incomplete_receipt",
        lambda *_args, **_kwargs: receipt,
    )
    source_set = SimpleNamespace(cognitive_durable_digest={})

    def missing(*_args, **_kwargs):  # noqa: ANN202
        raise RebuildSourceManifestNotFoundError("missing")

    bundle = SimpleNamespace(
        artifact_store=_ReceiptArtifactStore(audit),
        source_enumerator=SimpleNamespace(enumerate=lambda **_kwargs: source_set),
        manifest_store=SimpleNamespace(load_verified=missing),
    )

    with pytest.raises(
        recovery.RecoveryRefused,
        match="terminal_reconciliation_required",
    ):
        recovery._select_recovery_run_plan(
            bundle,
            board_id=BOARD_ID,
            actor_id="owner-1",
            raw_health={},
        )


@pytest.mark.parametrize("failure_kind", ("missing", "drift"))
def test_closed_failed_nonpromoted_audit_selects_receipt_only_archive(
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    from okto_pulse.core.kg.rebuild_sources import (
        RebuildSourceManifestNotFoundError,
        SourceSetRevalidation,
    )

    receipt = _receipt(state="authorized")
    audit = _terminal_audit(
        receipt,
        outcome="failed",
        reason="lifecycle_failed",
    )
    monkeypatch.setattr(
        recovery,
        "_discover_incomplete_receipt",
        lambda *_args, **_kwargs: receipt,
    )
    source_set = SimpleNamespace(cognitive_durable_digest={})
    manifest = SimpleNamespace(
        manifest_ref=receipt["manifest_ref"],
        source_set_hash=receipt["source_set_hash"],
    )

    def load_verified(*_args, **_kwargs):  # noqa: ANN202
        if failure_kind == "missing":
            raise RebuildSourceManifestNotFoundError("missing")
        return manifest

    bundle = SimpleNamespace(
        artifact_store=_ReceiptArtifactStore(audit),
        source_enumerator=SimpleNamespace(enumerate=lambda **_kwargs: source_set),
        manifest_store=SimpleNamespace(
            load_verified=load_verified,
            classify_revalidation=lambda **_kwargs: SimpleNamespace(
                outcome=SourceSetRevalidation.MANIFEST_DRIFT
            ),
        ),
    )

    plan = recovery._select_recovery_run_plan(
        bundle,
        board_id=BOARD_ID,
        actor_id="owner-1",
        raw_health={},
    )

    assert plan.mode == "archive_closed"
    assert plan.receipt is receipt
    assert plan.terminal_audit == audit
    assert plan.manifest.manifest_ref == receipt["manifest_ref"]
    assert plan.manifest_verification_failure


def test_closed_audit_with_irreversible_effect_never_archives_or_compensates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.core.kg.rebuild_sources import RebuildSourceManifestNotFoundError

    receipt = _receipt(state="authorized")
    audit = _terminal_audit(
        receipt,
        outcome="failed",
        reason="lifecycle_failed",
    )
    audit["report_ref"] = "report://already-visible"
    monkeypatch.setattr(
        recovery,
        "_discover_incomplete_receipt",
        lambda *_args, **_kwargs: receipt,
    )

    def missing(*_args, **_kwargs):  # noqa: ANN202
        raise RebuildSourceManifestNotFoundError("missing")

    bundle = SimpleNamespace(
        artifact_store=_ReceiptArtifactStore(audit),
        source_enumerator=SimpleNamespace(
            enumerate=lambda **_kwargs: SimpleNamespace(cognitive_durable_digest={})
        ),
        manifest_store=SimpleNamespace(load_verified=missing),
    )

    with pytest.raises(
        recovery.RecoveryRefused, match="terminal_reconciliation_required"
    ):
        recovery._select_recovery_run_plan(
            bundle,
            board_id=BOARD_ID,
            actor_id="owner-1",
            raw_health={},
        )


def _source_row(
    *,
    artifact_type: str = "spec",
    source_ref: str = "spec:source-1",
    artifact_id: str = "source-1",
    content_hash: str = "a" * 64,
    status: str = "published",
):
    payload = {
        "artifact_type": artifact_type,
        "source_ref": source_ref,
        "source_version": "7",
        "content_hash": content_hash,
        "created_at": "2026-08-15T10:00:00+00:00",
        "id": artifact_id,
        "source_artifact_status": status,
        "graph_layer": "canonical",
        "maturity_status": "canonical_eligible",
        "disposition": "canonical",
        "reason_code": "",
        "expires_at": None,
    }
    return SimpleNamespace(**payload, to_dict=lambda: dict(payload))


def test_rebaseline_event_resolver_uses_bound_live_v3_projection() -> None:
    from okto_pulse.core.kg.rebuild_sources import SourceSetRevalidation

    legacy = _source_row(content_hash="1" * 64)
    current = _source_row(content_hash="2" * 64)
    manifest = SimpleNamespace(materializable_sources=(legacy,))
    source_set = SimpleNamespace(
        board_id=BOARD_ID,
        materializable_sources=(current,),
        cognitive_durable_digest={"digest": "current"},
    )
    target_hash = "3" * 64

    class ManifestStore:
        def load_verified(self, manifest_ref, **kwargs):  # noqa: ANN001, ANN201
            assert manifest_ref == "manifest_legacy"
            assert kwargs == {
                "expected_board_id": BOARD_ID,
                "expected_preflight_hash": "4" * 64,
                "cognitive_digest": source_set.cognitive_durable_digest,
            }
            return manifest

        def classify_revalidation(self, **kwargs):  # noqa: ANN001, ANN201
            assert kwargs == {
                "manifest": manifest,
                "current_source_set": source_set,
            }
            return SimpleNamespace(
                outcome=SourceSetRevalidation.REBASELINE,
                to_source_set_hash=target_hash,
            )

    binding = recovery.EventManifestBinding(
        board_id=BOARD_ID,
        preflight_hash="4" * 64,
        rebaseline_evidence_id="run_legacy:manifest_legacy",
        rebaseline_target_source_set_hash=target_hash,
        rebaseline_materializable_sources=(current.to_dict(),),
    )
    payload = {
        "board_id": BOARD_ID,
        "run_id": "run_legacy",
        "manifest_ref": "manifest_legacy",
        "rebaseline_evidence_id": "run_legacy:manifest_legacy",
        "rebaseline_target_source_set_hash": target_hash,
    }

    resolved = recovery._resolve_event_cognitive_sources(
        binding=binding,
        event_payload=payload,
        manifest_store=ManifestStore(),
        source_enumerator=SimpleNamespace(enumerate=lambda **_kwargs: source_set),
    )

    assert resolved == (current.to_dict(),)
    assert resolved[0]["content_hash"] == "2" * 64
    assert resolved[0]["content_hash"] != legacy.to_dict()["content_hash"]

    with pytest.raises(RuntimeError, match="rebaseline_binding_mismatch"):
        recovery._resolve_event_cognitive_sources(
            binding=binding,
            event_payload={**payload, "rebaseline_evidence_id": "forged"},
            manifest_store=ManifestStore(),
            source_enumerator=SimpleNamespace(enumerate=lambda **_kwargs: source_set),
        )


def test_non_rebaseline_event_requires_equivalent_source_set() -> None:
    from okto_pulse.core.kg.rebuild_sources import SourceSetRevalidation

    row = _source_row()
    manifest = SimpleNamespace(materializable_sources=(row,))
    source_set = SimpleNamespace(
        materializable_sources=(row,),
        cognitive_durable_digest={},
    )
    binding = recovery.EventManifestBinding(
        board_id=BOARD_ID,
        preflight_hash="4" * 64,
    )
    store = SimpleNamespace(
        load_verified=lambda *_args, **_kwargs: manifest,
        classify_revalidation=lambda **_kwargs: SimpleNamespace(
            outcome=SourceSetRevalidation.MANIFEST_DRIFT
        ),
    )

    with pytest.raises(RuntimeError, match="recovery_event_source_drift"):
        recovery._resolve_event_cognitive_sources(
            binding=binding,
            event_payload={
                "board_id": BOARD_ID,
                "run_id": "run_current",
                "manifest_ref": "manifest_current",
            },
            manifest_store=store,
            source_enumerator=SimpleNamespace(enumerate=lambda **_kwargs: source_set),
        )


def test_rebaseline_expected_command_rows_use_live_v3_hash_and_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.community.adapters import board_rebuild_ingestion

    legacy = _source_row(content_hash="1" * 64)
    current = _source_row(content_hash="2" * 64)
    legacy_evidence = _source_row(
        artifact_type="code_evidence",
        source_ref="code_evidence:evidence-1",
        artifact_id="evidence-1",
        content_hash="3" * 64,
        status="superseded",
    )
    current_evidence = _source_row(
        artifact_type="code_evidence",
        source_ref="code_evidence:evidence-1",
        artifact_id="evidence-1",
        content_hash="4" * 64,
        status="superseded",
    )
    manifest = SimpleNamespace(
        created_at="2026-08-14T10:00:00+00:00",
        materializable_sources=(legacy,),
        skipped_expired_working=(legacy_evidence,),
    )
    source_set = SimpleNamespace(
        board_id=BOARD_ID,
        materializable_sources=(current,),
        skipped_expired_working=(current_evidence,),
    )
    evidence_id = "run_rebaseline:manifest_legacy"
    monkeypatch.setattr(
        board_rebuild_ingestion,
        "_resolve_evidence_dependency_closure",
        lambda **kwargs: (tuple(kwargs["sources"]), 1),
    )

    rows, closure_count = recovery._resolve_expected_command_sources(
        tmp_path / "copy.db",
        board_id=BOARD_ID,
        manifest=manifest,
        rebaseline_source_set=source_set,
        rebaseline_evidence_id=evidence_id,
    )

    assert closure_count == 1
    assert [row["content_hash"] for row in rows] == ["2" * 64, "4" * 64]
    assert all(row["_rebuild_rebaseline_evidence_id"] == evidence_id for row in rows)
    assert all(
        row["_rebuild_manifest_created_at"] == manifest.created_at for row in rows
    )
    assert rows[1]["_rebuild_dependency_closure_candidate"] == (
        "code_evidence_supersedence"
    )


def _create_queue_db(path: Path) -> None:
    import sqlite3

    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE consolidation_queue ("
            "id TEXT PRIMARY KEY, board_id TEXT, artifact_type TEXT, "
            "artifact_id TEXT, work_kind TEXT, generation INTEGER, payload TEXT, "
            "delete_event_id TEXT, priority INTEGER, source TEXT, status TEXT, "
            "triggered_at TEXT, claimed_by_session_id TEXT, claim_token TEXT, "
            "claimed_at TEXT, last_error TEXT, worker_id TEXT, "
            "claim_timeout_at TEXT, attempts INTEGER, next_retry_at TEXT, "
            "triggered_by_event TEXT)"
        )


_EXACT_QUEUE_COLUMNS = (
    "id",
    "artifact_type",
    "artifact_id",
    "status",
    "priority",
    "attempts",
    "last_error",
    "next_retry_at",
    "claimed_at",
    "claim_timeout_at",
    "worker_id",
    "claimed_by_session_id",
    "claim_token",
    "generation",
    "delete_event_id",
    "work_kind",
    "source",
    "triggered_at",
    "payload",
)


def _exact_source_row() -> dict[str, object]:
    return {
        "artifact_type": "spec",
        "id": "exact-spec",
        "source_ref": "spec:exact-spec",
        "source_version": "7",
        "content_hash": "a" * 64,
    }


def _exact_membership(manifest_ref: str) -> dict[str, object]:
    source_row = _exact_source_row()
    return {
        "run_id": manifest_ref,
        "source_ref": source_row["source_ref"],
        "source_version": source_row["source_version"],
        "content_hash": source_row["content_hash"],
    }


def _exact_marker(
    *,
    manifest_ref: str,
    reservation_lineage_id: str,
    disposition: str = "retry_scheduled",
    attempt_ordinal: int = 1,
) -> dict[str, object]:
    retry_at = "2026-08-15T12:00:01+00:00" if disposition == "retry_scheduled" else None
    return {
        "schema_version": 1,
        "queue_id": "exact-row",
        "board_id": BOARD_ID,
        "source": f"rebuild:{manifest_ref}",
        "reservation_lineage_id": reservation_lineage_id,
        "work_kind": "consolidate",
        "artifact_type": "spec",
        "artifact_id": "exact-spec",
        "generation": 0,
        "membership_source_ref": "spec:exact-spec",
        "membership_source_version": "7",
        "membership_content_hash": "a" * 64,
        "attempt_ordinal": attempt_ordinal,
        "queue_attempts": attempt_ordinal,
        "disposition": disposition,
        "retryable": disposition == "retry_scheduled",
        "mutation_state": "unchanged",
        "error_code": "connectivity_constraint_violated",
        "error_message": "deterministic exact failure",
        "next_retry_at": retry_at,
        "diagnostic_json": None,
    }


def _exact_queue_snapshot(
    *,
    manifest_ref: str,
    marker: Mapping[str, object] | None,
    status: str = "pending",
) -> recovery.QueueSnapshot:
    retry_at = marker.get("next_retry_at") if marker is not None else None
    attempts = marker.get("attempt_ordinal") if marker is not None else 0
    payload: dict[str, object] = {
        "_rebuild_membership": _exact_membership(manifest_ref)
    }
    if marker is not None:
        payload["_exact_rebuild_disposition"] = dict(marker)
    claimed = status == "claimed"
    row = (
        "exact-row",
        "spec",
        "exact-spec",
        status,
        "high",
        attempts,
        (
            "connectivity_constraint_violated:deterministic exact failure"
            if marker is not None
            else None
        ),
        retry_at,
        "2026-08-15T12:00:00+00:00" if claimed else None,
        "2026-08-15T12:05:00+00:00" if claimed else None,
        "worker-exact" if claimed else None,
        "worker-exact" if claimed else None,
        "1" * 32 if claimed else None,
        0,
        None,
        "consolidate",
        f"rebuild:{manifest_ref}",
        "2026-08-15T11:59:00+00:00",
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
    )
    return recovery.QueueSnapshot(_EXACT_QUEUE_COLUMNS, (row,), "exact-cut")


def test_exact_disposition_admission_binds_verified_lineage_and_resume_shape() -> None:
    manifest_ref = "manifest-exact-lineage"
    confirmation_ref = f"conf_fp_{'b' * 64}"
    lineage_id = recovery._exact_reservation_lineage_id(
        board_id=BOARD_ID,
        manifest_ref=manifest_ref,
        f06_run_id=f"f06:{manifest_ref}",
        confirmation_ref=confirmation_ref,
    )
    marker = _exact_marker(
        manifest_ref=manifest_ref,
        reservation_lineage_id=lineage_id,
    )
    pending = _exact_queue_snapshot(manifest_ref=manifest_ref, marker=marker)

    with pytest.raises(
        recovery.RecoveryRefused,
        match="rebuild_queue_exact_disposition_not_authorized",
    ):
        recovery._assert_exact_queue_admission(
            pending,
            board_id=BOARD_ID,
            manifest_ref=manifest_ref,
            reservation_lineage_id=lineage_id,
            source_rows=(_exact_source_row(),),
            ordered_source_rows=(_exact_source_row(),),
        )

    assert (
        recovery._assert_exact_queue_admission(
            pending,
            board_id=BOARD_ID,
            manifest_ref=manifest_ref,
            reservation_lineage_id=lineage_id,
            source_rows=(_exact_source_row(),),
            ordered_source_rows=(_exact_source_row(),),
            allow_consumed_prefix=True,
            allow_durable_dispositions=True,
        )
        == 0
    )
    claimed = _exact_queue_snapshot(
        manifest_ref=manifest_ref,
        marker=marker,
        status="claimed",
    )
    assert (
        recovery._assert_exact_queue_admission(
            claimed,
            board_id=BOARD_ID,
            manifest_ref=manifest_ref,
            reservation_lineage_id=lineage_id,
            source_rows=(_exact_source_row(),),
            ordered_source_rows=(_exact_source_row(),),
            allow_consumed_prefix=True,
            allow_claimed_recovery=True,
            allow_durable_dispositions=True,
        )
        == 1
    )
    neutral_claim = _exact_queue_snapshot(
        manifest_ref=manifest_ref,
        marker=None,
        status="claimed",
    )
    assert (
        recovery._assert_exact_queue_admission(
            neutral_claim,
            board_id=BOARD_ID,
            manifest_ref=manifest_ref,
            reservation_lineage_id=lineage_id,
            source_rows=(_exact_source_row(),),
            ordered_source_rows=(_exact_source_row(),),
            allow_consumed_prefix=True,
            allow_claimed_recovery=True,
            allow_durable_dispositions=True,
        )
        == 1
    )

    tampered = dict(marker)
    tampered["reservation_lineage_id"] = "c" * 64
    with pytest.raises(
        recovery.RecoveryRefused,
        match="rebuild_queue_exact_disposition_binding_invalid",
    ):
        recovery._assert_exact_queue_admission(
            _exact_queue_snapshot(manifest_ref=manifest_ref, marker=tampered),
            board_id=BOARD_ID,
            manifest_ref=manifest_ref,
            reservation_lineage_id=lineage_id,
            source_rows=(_exact_source_row(),),
            ordered_source_rows=(_exact_source_row(),),
            allow_consumed_prefix=True,
            allow_durable_dispositions=True,
        )


def test_exact_batch_result_is_bijective_with_durable_marker() -> None:
    from okto_pulse.core.ports.consolidation import (
        ConsolidationClaimScope,
        ExactConsolidationBatchResult,
        ExactConsolidationDisposition,
        ExactConsolidationMutationState,
        ExactConsolidationResultOrigin,
        ExactConsolidationRowResult,
    )

    manifest_ref = "manifest-exact-result"
    lineage_id = recovery._exact_reservation_lineage_id(
        board_id=BOARD_ID,
        manifest_ref=manifest_ref,
        f06_run_id=f"f06:{manifest_ref}",
        confirmation_ref=f"conf_fp_{'d' * 64}",
    )
    scope = ConsolidationClaimScope(
        board_id=BOARD_ID,
        source=f"rebuild:{manifest_ref}",
        reservation_lineage_id=lineage_id,
    )
    marker = _exact_marker(
        manifest_ref=manifest_ref,
        reservation_lineage_id=lineage_id,
        disposition="terminal_failure",
    )
    row = ExactConsolidationRowResult(
        queue_id="exact-row",
        board_id=BOARD_ID,
        source=scope.source,
        reservation_lineage_id=lineage_id,
        work_kind="consolidate",
        artifact_type="spec",
        artifact_id="exact-spec",
        generation=0,
        membership_source_ref="spec:exact-spec",
        membership_source_version="7",
        membership_content_hash="a" * 64,
        attempt_ordinal=1,
        disposition=ExactConsolidationDisposition.TERMINAL_FAILURE,
        origin=ExactConsolidationResultOrigin.NEW,
        mutation_state=ExactConsolidationMutationState.UNCHANGED,
        error_code="connectivity_constraint_violated",
        error_message="deterministic exact failure",
    )
    result = ExactConsolidationBatchResult(claim_scope=scope, rows=(row,))
    before = _exact_queue_snapshot(manifest_ref=manifest_ref, marker=None)
    after = _exact_queue_snapshot(manifest_ref=manifest_ref, marker=marker)
    processor = SimpleNamespace(last_attempted_count=1)

    assert recovery._assert_exact_batch_result(
        result,
        processor,
        scope,
        before,
        after,
        board_id=BOARD_ID,
        source_rows=(_exact_source_row(),),
    ) == (row,)

    tampered = dict(marker)
    tampered["error_message"] = "forged terminal meaning"
    with pytest.raises(
        recovery.RecoveryRefused,
        match="exact_processor_disposition_result_mismatch",
    ):
        recovery._assert_exact_batch_result(
            result,
            processor,
            scope,
            before,
            _exact_queue_snapshot(manifest_ref=manifest_ref, marker=tampered),
            board_id=BOARD_ID,
            source_rows=(_exact_source_row(),),
        )


@pytest.mark.parametrize("source_artifact_type", ("task", "test", "bug"))
def test_exact_batch_preserves_card_source_ref_alias(
    source_artifact_type: str,
) -> None:
    from dataclasses import replace

    from okto_pulse.core.ports.consolidation import (
        ConsolidationClaimScope,
        ExactConsolidationBatchResult,
        ExactConsolidationDisposition,
        ExactConsolidationMutationState,
        ExactConsolidationResultOrigin,
        ExactConsolidationRowResult,
    )

    manifest_ref = f"manifest-exact-{source_artifact_type}"
    source = f"rebuild:{manifest_ref}"
    lineage_id = "7" * 64
    source_row = {
        "artifact_type": source_artifact_type,
        "id": "card-source",
        "source_ref": f"{source_artifact_type}:card-source",
        "source_version": "2",
        "content_hash": "6" * 64,
    }
    payload = {
        "_rebuild_membership": {
            "run_id": manifest_ref,
            "source_ref": source_row["source_ref"],
            "source_version": source_row["source_version"],
            "content_hash": source_row["content_hash"],
        }
    }
    before_row = (
        "card-row",
        "card",
        "card-source",
        "pending",
        "high",
        0,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        0,
        None,
        "consolidate",
        source,
        "2026-08-15T11:59:00+00:00",
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
    )
    before = recovery.QueueSnapshot(_EXACT_QUEUE_COLUMNS, (before_row,), "card-cut")
    after = recovery.QueueSnapshot(_EXACT_QUEUE_COLUMNS, (), "card-acked")
    scope = ConsolidationClaimScope(
        board_id=BOARD_ID,
        source=source,
        reservation_lineage_id=lineage_id,
    )
    ack = ExactConsolidationRowResult(
        queue_id="card-row",
        board_id=BOARD_ID,
        source=source,
        reservation_lineage_id=lineage_id,
        work_kind="consolidate",
        artifact_type="card",
        artifact_id="card-source",
        generation=0,
        membership_source_ref=str(source_row["source_ref"]),
        membership_source_version="2",
        membership_content_hash="6" * 64,
        attempt_ordinal=1,
        disposition=ExactConsolidationDisposition.ACKED,
        origin=ExactConsolidationResultOrigin.NEW,
        mutation_state=ExactConsolidationMutationState.COMMITTED,
    )

    assert recovery._assert_exact_batch_result(
        ExactConsolidationBatchResult(claim_scope=scope, rows=(ack,)),
        SimpleNamespace(last_attempted_count=1),
        scope,
        before,
        after,
        board_id=BOARD_ID,
        source_rows=(source_row,),
    ) == (ack,)
    with pytest.raises(ValueError, match="exact_consolidation_row_binding_invalid"):
        replace(ack, membership_source_ref="spec:card-source")


@pytest.mark.asyncio
async def test_exact_drain_retries_typed_marker_then_cancels_on_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from okto_pulse.core.ports.consolidation import (
        ConsolidationClaimScope,
        ExactConsolidationBatchResult,
        ExactConsolidationDisposition,
        ExactConsolidationMutationState,
        ExactConsolidationResultOrigin,
        ExactConsolidationRowResult,
    )

    manifest_ref = "manifest-exact-drain"
    source = f"rebuild:{manifest_ref}"
    lineage_id = recovery._exact_reservation_lineage_id(
        board_id=BOARD_ID,
        manifest_ref=manifest_ref,
        f06_run_id=f"f06:{manifest_ref}",
        confirmation_ref=f"conf_fp_{'e' * 64}",
    )
    scope = ConsolidationClaimScope(
        board_id=BOARD_ID,
        source=source,
        reservation_lineage_id=lineage_id,
    )
    retry_marker = _exact_marker(
        manifest_ref=manifest_ref,
        reservation_lineage_id=lineage_id,
    )
    terminal_marker = _exact_marker(
        manifest_ref=manifest_ref,
        reservation_lineage_id=lineage_id,
        disposition="terminal_failure",
        attempt_ordinal=2,
    )
    snapshots = iter(
        (
            _exact_queue_snapshot(manifest_ref=manifest_ref, marker=None),
            _exact_queue_snapshot(manifest_ref=manifest_ref, marker=retry_marker),
            _exact_queue_snapshot(manifest_ref=manifest_ref, marker=retry_marker),
            _exact_queue_snapshot(manifest_ref=manifest_ref, marker=terminal_marker),
        )
    )

    def disposition_row(
        disposition: ExactConsolidationDisposition,
        *,
        attempt: int,
    ) -> ExactConsolidationRowResult:
        return ExactConsolidationRowResult(
            queue_id="exact-row",
            board_id=BOARD_ID,
            source=source,
            reservation_lineage_id=lineage_id,
            work_kind="consolidate",
            artifact_type="spec",
            artifact_id="exact-spec",
            generation=0,
            membership_source_ref="spec:exact-spec",
            membership_source_version="7",
            membership_content_hash="a" * 64,
            attempt_ordinal=attempt,
            disposition=disposition,
            origin=ExactConsolidationResultOrigin.NEW,
            mutation_state=ExactConsolidationMutationState.UNCHANGED,
            error_code="connectivity_constraint_violated",
            error_message="deterministic exact failure",
            next_retry_at=(
                datetime(2026, 8, 15, 12, 0, 1, tzinfo=timezone.utc)
                if disposition is ExactConsolidationDisposition.RETRY_SCHEDULED
                else None
            ),
        )

    class Processor:
        last_attempted_count = 0
        calls = 0

        async def process_exact_batch(
            self, *, claim_scope, reservation_authority_probe
        ):  # noqa: ANN001, ANN201
            assert claim_scope == scope
            assert reservation_authority_probe() is True
            self.calls += 1
            self.last_attempted_count = 1
            disposition = (
                ExactConsolidationDisposition.RETRY_SCHEDULED
                if self.calls == 1
                else ExactConsolidationDisposition.TERMINAL_FAILURE
            )
            return ExactConsolidationBatchResult(
                claim_scope=scope,
                rows=(disposition_row(disposition, attempt=self.calls),),
            )

    reservation = SimpleNamespace(
        admin_lane=True,
        operation=f"kg02_rebuild_reservation:{manifest_ref}",
        expires_at_epoch=__import__("time").time() + 60,
        owner_token="reservation-token",
        owner_id="reservation-owner",
        acquired_at_epoch=1.0,
    )

    class ReservationPort:
        def inspect(self, *, board_id: str):
            assert board_id == BOARD_ID
            return reservation

        def is_owner(self, *, board_id: str, owner_token: str) -> bool:
            return board_id == BOARD_ID and owner_token == reservation.owner_token

    baseline = recovery.QueueSnapshot((), (), "stable")
    cancel_event = __import__("threading").Event()

    async def service_run() -> object:
        while not cancel_event.is_set():
            await asyncio.sleep(0)
        return SimpleNamespace(outcome="failed", reason="lifecycle_failed")

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        recovery, "_exact_queue_rows", lambda *_a, **_k: next(snapshots)
    )
    monkeypatch.setattr(recovery, "_active_exact_queue_depth", lambda *_a, **_k: 1)
    monkeypatch.setattr(recovery, "_assert_source_unchanged", lambda *_a, **_k: None)
    monkeypatch.setattr(recovery, "_dlq_snapshot", lambda *_a, **_k: baseline)
    monkeypatch.setattr(
        recovery, "_canonical_debt_snapshot", lambda *_a, **_k: baseline
    )
    monkeypatch.setattr(
        recovery, "_protected_queue_snapshot", lambda *_a, **_k: baseline
    )
    processor = Processor()
    service_task = asyncio.create_task(service_run())

    outcome = await recovery._drain_exact_scope(
        service_task,
        processor,
        scope,
        SimpleNamespace(operation_reservation=ReservationPort()),
        SimpleNamespace(manifest_ref=manifest_ref),
        board_id=BOARD_ID,
        source=source,
        baseline_dlq=baseline,
        baseline_debt=baseline,
        baseline_non_target=baseline,
        admitted_identities={("spec", "exact-spec")},
        source_rows=(_exact_source_row(),),
        ordered_source_rows=(_exact_source_row(),),
        cancel_event=cancel_event,
        lifetime_probe=lambda: True,
        timeout_seconds=1.0,
        poll_seconds=0.001,
    )

    assert processor.calls == 2
    assert cancel_event.is_set()
    assert outcome.service_result.outcome == "failed"
    assert outcome.blocker is not None
    assert outcome.blocker.kind == ExactConsolidationDisposition.TERMINAL_FAILURE.value
    assert outcome.blocker.queue_id == "exact-row"


@pytest.mark.asyncio
async def test_exact_drain_cancels_on_neutral_claim_loss_with_claim_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import threading
    import time

    from okto_pulse.core.ports.consolidation import (
        ConsolidationClaimScope,
        ExactConsolidationBatchResult,
        ExactConsolidationDisposition,
        ExactConsolidationMutationState,
        ExactConsolidationResultOrigin,
        ExactConsolidationRowResult,
    )

    manifest_ref = "manifest-exact-neutral"
    source = f"rebuild:{manifest_ref}"
    lineage_id = recovery._exact_reservation_lineage_id(
        board_id=BOARD_ID,
        manifest_ref=manifest_ref,
        f06_run_id=f"f06:{manifest_ref}",
        confirmation_ref=f"conf_fp_{'7' * 64}",
    )
    scope = ConsolidationClaimScope(
        board_id=BOARD_ID,
        source=source,
        reservation_lineage_id=lineage_id,
    )
    neutral = ExactConsolidationRowResult(
        queue_id="exact-row",
        board_id=BOARD_ID,
        source=source,
        reservation_lineage_id=lineage_id,
        work_kind="consolidate",
        artifact_type="spec",
        artifact_id="exact-spec",
        generation=0,
        membership_source_ref="spec:exact-spec",
        membership_source_version="7",
        membership_content_hash="a" * 64,
        attempt_ordinal=1,
        disposition=ExactConsolidationDisposition.NEUTRAL_FENCE_LOSS,
        origin=ExactConsolidationResultOrigin.NEW,
        mutation_state=ExactConsolidationMutationState.UNCHANGED,
        error_code="exact_consolidation_claim_lost",
        error_message="claim authority was lost before the exact transition",
    )

    class Processor:
        last_attempted_count = 1

        async def process_exact_batch(
            self, *, claim_scope, reservation_authority_probe
        ):  # noqa: ANN001, ANN201
            assert claim_scope == scope
            assert reservation_authority_probe() is True
            return ExactConsolidationBatchResult(claim_scope=scope, rows=(neutral,))

    reservation = SimpleNamespace(
        admin_lane=True,
        operation=f"kg02_rebuild_reservation:{manifest_ref}",
        expires_at_epoch=time.time() + 60,
        owner_token="reservation-token",
        owner_id="reservation-owner",
        acquired_at_epoch=1.0,
    )

    class ReservationPort:
        def inspect(self, *, board_id: str):
            assert board_id == BOARD_ID
            return reservation

        def is_owner(self, *, board_id: str, owner_token: str) -> bool:
            return board_id == BOARD_ID and owner_token == reservation.owner_token

    snapshots = iter(
        (
            _exact_queue_snapshot(manifest_ref=manifest_ref, marker=None),
            _exact_queue_snapshot(
                manifest_ref=manifest_ref,
                marker=None,
                status="claimed",
            ),
        )
    )
    baseline = recovery.QueueSnapshot((), (), "stable")
    cancel_event = threading.Event()

    async def service_run() -> object:
        while not cancel_event.is_set():
            await asyncio.sleep(0)
        return SimpleNamespace(outcome="failed", reason="lifecycle_failed")

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        recovery, "_exact_queue_rows", lambda *_a, **_k: next(snapshots)
    )
    monkeypatch.setattr(recovery, "_active_exact_queue_depth", lambda *_a, **_k: 1)
    monkeypatch.setattr(recovery, "_assert_source_unchanged", lambda *_a, **_k: None)
    monkeypatch.setattr(recovery, "_dlq_snapshot", lambda *_a, **_k: baseline)
    monkeypatch.setattr(
        recovery, "_canonical_debt_snapshot", lambda *_a, **_k: baseline
    )
    monkeypatch.setattr(
        recovery, "_protected_queue_snapshot", lambda *_a, **_k: baseline
    )
    service_task = asyncio.create_task(service_run())

    outcome = await recovery._drain_exact_scope(
        service_task,
        Processor(),
        scope,
        SimpleNamespace(operation_reservation=ReservationPort()),
        SimpleNamespace(manifest_ref=manifest_ref),
        board_id=BOARD_ID,
        source=source,
        baseline_dlq=baseline,
        baseline_debt=baseline,
        baseline_non_target=baseline,
        admitted_identities={("spec", "exact-spec")},
        source_rows=(_exact_source_row(),),
        ordered_source_rows=(_exact_source_row(),),
        cancel_event=cancel_event,
        lifetime_probe=lambda: True,
        timeout_seconds=1.0,
        poll_seconds=0.001,
    )

    assert cancel_event.is_set()
    assert outcome.service_result.outcome == "failed"
    assert outcome.blocker is not None
    assert outcome.blocker.kind == "neutral_fence_loss"
    assert outcome.blocker.queue_id == neutral.queue_id
    assert outcome.blocker.mutation_state == "unchanged"
    assert outcome.blocker.row_result == neutral


@pytest.mark.asyncio
async def test_exact_drain_validates_partial_ack_then_compensates_post_commit_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import threading
    import time

    from okto_pulse.core.ports.consolidation import (
        ConsolidationClaimScope,
        ExactConsolidationBatchResult,
        ExactConsolidationDisposition,
        ExactConsolidationMutationState,
        ExactConsolidationPostCommitError,
        ExactConsolidationResultOrigin,
        ExactConsolidationRowResult,
    )

    manifest_ref = "manifest-exact-post-commit"
    source = f"rebuild:{manifest_ref}"
    lineage_id = recovery._exact_reservation_lineage_id(
        board_id=BOARD_ID,
        manifest_ref=manifest_ref,
        f06_run_id=f"f06:{manifest_ref}",
        confirmation_ref=f"conf_fp_{'f' * 64}",
    )
    scope = ConsolidationClaimScope(
        board_id=BOARD_ID,
        source=source,
        reservation_lineage_id=lineage_id,
    )
    ack = ExactConsolidationRowResult(
        queue_id="exact-row",
        board_id=BOARD_ID,
        source=source,
        reservation_lineage_id=lineage_id,
        work_kind="consolidate",
        artifact_type="spec",
        artifact_id="exact-spec",
        generation=0,
        membership_source_ref="spec:exact-spec",
        membership_source_version="7",
        membership_content_hash="a" * 64,
        attempt_ordinal=1,
        disposition=ExactConsolidationDisposition.ACKED,
        origin=ExactConsolidationResultOrigin.NEW,
        mutation_state=ExactConsolidationMutationState.COMMITTED,
    )
    partial = ExactConsolidationBatchResult(claim_scope=scope, rows=(ack,))
    post_commit_error = ExactConsolidationPostCommitError(
        batch_result=partial,
        failed_queue_id=ack.queue_id,
        error_code="exact_consolidation_post_commit_finalization_failed",
    )

    class Processor:
        last_attempted_count = 1

        async def process_exact_batch(
            self, *, claim_scope, reservation_authority_probe
        ):  # noqa: ANN001, ANN201
            assert claim_scope == scope
            assert reservation_authority_probe() is True
            raise post_commit_error

    reservation = SimpleNamespace(
        admin_lane=True,
        operation=f"kg02_rebuild_reservation:{manifest_ref}",
        expires_at_epoch=time.time() + 60,
        owner_token="reservation-token",
        owner_id="reservation-owner",
        acquired_at_epoch=1.0,
    )

    class ReservationPort:
        def inspect(self, *, board_id: str):
            assert board_id == BOARD_ID
            return reservation

        def is_owner(self, *, board_id: str, owner_token: str) -> bool:
            return board_id == BOARD_ID and owner_token == reservation.owner_token

    snapshots = iter(
        (
            _exact_queue_snapshot(manifest_ref=manifest_ref, marker=None),
            recovery.QueueSnapshot(_EXACT_QUEUE_COLUMNS, (), "acked-cut"),
        )
    )
    depths = iter((1, 0))
    baseline = recovery.QueueSnapshot((), (), "stable")
    cancel_event = threading.Event()

    async def service_run() -> object:
        while not cancel_event.is_set():
            await asyncio.sleep(0)
        return SimpleNamespace(outcome="failed", reason="lifecycle_failed")

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        recovery, "_exact_queue_rows", lambda *_a, **_k: next(snapshots)
    )
    monkeypatch.setattr(
        recovery,
        "_active_exact_queue_depth",
        lambda *_a, **_k: next(depths),
    )
    monkeypatch.setattr(recovery, "_assert_source_unchanged", lambda *_a, **_k: None)
    monkeypatch.setattr(recovery, "_dlq_snapshot", lambda *_a, **_k: baseline)
    monkeypatch.setattr(
        recovery, "_canonical_debt_snapshot", lambda *_a, **_k: baseline
    )
    monkeypatch.setattr(
        recovery, "_protected_queue_snapshot", lambda *_a, **_k: baseline
    )
    service_task = asyncio.create_task(service_run())

    outcome = await recovery._drain_exact_scope(
        service_task,
        Processor(),
        scope,
        SimpleNamespace(operation_reservation=ReservationPort()),
        SimpleNamespace(manifest_ref=manifest_ref),
        board_id=BOARD_ID,
        source=source,
        baseline_dlq=baseline,
        baseline_debt=baseline,
        baseline_non_target=baseline,
        admitted_identities={("spec", "exact-spec")},
        source_rows=(_exact_source_row(),),
        ordered_source_rows=(_exact_source_row(),),
        cancel_event=cancel_event,
        lifetime_probe=lambda: True,
        timeout_seconds=1.0,
        poll_seconds=0.001,
    )

    assert cancel_event.is_set()
    assert outcome.blocker is not None
    assert outcome.blocker.kind == "post_commit_error"
    assert outcome.blocker.queue_id == ack.queue_id
    assert outcome.blocker.mutation_state == "committed"
    assert outcome.blocker.row_result == ack


def test_exact_post_commit_blocker_passes_full_compensation_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.core.ports.consolidation import (
        ConsolidationClaimScope,
        ExactConsolidationDisposition,
        ExactConsolidationMutationState,
        ExactConsolidationResultOrigin,
        ExactConsolidationRowResult,
    )

    manifest_ref = "manifest-post-commit-gate"
    source = f"rebuild:{manifest_ref}"
    scope = ConsolidationClaimScope(
        board_id=BOARD_ID,
        source=source,
        reservation_lineage_id="9" * 64,
    )
    ack = ExactConsolidationRowResult(
        queue_id="post-commit-row",
        board_id=BOARD_ID,
        source=source,
        reservation_lineage_id=str(scope.reservation_lineage_id),
        work_kind="consolidate",
        artifact_type="spec",
        artifact_id="post-commit-spec",
        generation=0,
        membership_source_ref="spec:post-commit-spec",
        membership_source_version="1",
        membership_content_hash="8" * 64,
        attempt_ordinal=1,
        disposition=ExactConsolidationDisposition.ACKED,
        origin=ExactConsolidationResultOrigin.NEW,
        mutation_state=ExactConsolidationMutationState.COMMITTED,
    )
    blocker = recovery.ExactDrainBlocker(
        kind="post_commit_error",
        queue_id=ack.queue_id,
        mutation_state="committed",
        error_code="exact_consolidation_post_commit_finalization_failed",
        row_result=ack,
    )
    actions = (
        "cancel_enqueued_sources",
        "discard_candidate_generation",
        "restore_quarantine",
    )
    receipts: dict[str, dict[str, object]] = {}
    for effect in ("snapshot", "quarantine", "enqueue"):
        effect_key = f"f06:{manifest_ref}:{effect}"
        receipts[effect_key] = {
            "effect_key": effect_key,
            "effect": effect,
            "ok": True,
            "code": "ok",
            "details": (
                {"quarantine_ref": "q_original"} if effect == "quarantine" else {}
            ),
        }
    compensate_key = f"f06:{manifest_ref}:compensate"
    receipts[compensate_key] = {
        "effect_key": compensate_key,
        "effect": "compensate",
        "ok": True,
        "code": "compensated",
        "details": {
            "actions": list(actions),
            "queue": {"active_remaining": 0},
            "quarantine_restore": {
                "ok": True,
                "report": {
                    "applied": True,
                    "open_validated": True,
                    "board_id": BOARD_ID,
                    "backup_quarantine_id": "q_backup",
                },
            },
            "candidate_discard": {"status": "discarded"},
        },
    }
    checkpoint = {
        "state": "failed",
        "compensation_failed_state": "draining",
        "compensation_failure_code": "cancelled",
        "compensation_failure_detail": "cancellation requested",
        "compensation_actions": list(actions),
        "writer_handoff_count": 1,
        "writer_reacquire_count": 1,
        "command": {"board_id": BOARD_ID, "manifest_ref": manifest_ref},
        "receipts": receipts,
    }
    run_id = "run_post_commit_gate"
    audit_ref = "audit://post-commit-gate"
    audit = {
        "run_id": run_id,
        "board_id": BOARD_ID,
        "manifest_ref": manifest_ref,
        "outcome": "failed",
        "reason": "lifecycle_failed",
        "current_kg_generation_id": None,
        "promotion_outcome": "not_promoted",
        "publishable_status": "failed",
        "event_emitted": False,
    }

    class ArtifactStore:
        def read_json_reference(self, reference: str):
            assert reference == audit_ref
            return audit

        def read_json(self, key):  # noqa: ANN001, ANN201
            from okto_pulse.community.adapters.rebuild_effects import (
                CommunityRebuildEffects,
            )

            for effect_key, receipt in receipts.items():
                if key.artifact_id == CommunityRebuildEffects._effect_id(effect_key):
                    return receipt
            return None

    no_lock = SimpleNamespace(inspect=lambda **_kwargs: None)
    bundle = SimpleNamespace(
        artifact_store=ArtifactStore(),
        single_writer_lock=no_lock,
        operation_reservation=no_lock,
    )
    result = SimpleNamespace(
        run_id=run_id,
        audit_ref=audit_ref,
        outcome="failed",
        reason="lifecycle_failed",
        current_kg_generation_id=None,
        promotion_outcome="not_promoted",
        publishable_status="failed",
        event_emitted=False,
    )
    baseline = recovery.QueueSnapshot((), (), "stable")
    quarantine_root = tmp_path / "quarantine"
    board_storage_root = tmp_path / "boards" / BOARD_ID
    quarantine_root.mkdir()
    board_storage_root.mkdir(parents=True)
    health = {
        "current_kg_generation_id": None,
        "graph_storage_exists": True,
    }
    monkeypatch.setattr(recovery, "_load_checkpoint", lambda *_a, **_k: checkpoint)
    monkeypatch.setattr(recovery, "_active_exact_queue_depth", lambda *_a, **_k: 0)
    monkeypatch.setattr(recovery, "_active_rebuild_queue_depth", lambda *_a, **_k: 0)
    monkeypatch.setattr(
        recovery,
        "_compensated_queue_adoption_from_checkpoint",
        lambda *_a, **_k: object(),
    )
    monkeypatch.setattr(
        recovery,
        "_assert_protected_admission_is_noop",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(recovery, "_dlq_snapshot", lambda *_a, **_k: baseline)
    monkeypatch.setattr(
        recovery, "_canonical_debt_snapshot", lambda *_a, **_k: baseline
    )
    monkeypatch.setattr(
        recovery, "_protected_queue_snapshot", lambda *_a, **_k: baseline
    )
    monkeypatch.setattr(recovery, "_assert_tree_preserved", lambda *_a, **_k: None)
    monkeypatch.setattr(
        recovery,
        "_quarantine_ids",
        lambda *_a, **_k: {"q_original", "q_backup"},
    )
    monkeypatch.setattr(
        recovery,
        "_snapshot_closed_board_storage",
        lambda **_kwargs: {"graph.lbug": "a" * 64},
    )
    monkeypatch.setattr(
        recovery,
        "_offline_cold_graph_health",
        lambda *_a, **_k: dict(health),
    )

    recovery._assert_exact_blocking_compensation(
        bundle,
        SimpleNamespace(manifest_ref=manifest_ref),
        result,
        blocker,
        board_id=BOARD_ID,
        db_path=tmp_path / "pulse.db",
        source=source,
        baseline_dlq=baseline,
        baseline_debt=baseline,
        baseline_non_target=baseline,
        admitted_identities={("spec", "post-commit-spec")},
        quarantine_root=quarantine_root,
        quarantine_baseline={},
        quarantine_baseline_ids=frozenset(),
        board_storage_root=board_storage_root,
        baseline_health=health,
    )


@pytest.mark.asyncio
async def test_drain_waiter_requires_durable_writer_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    checkpoint: dict[str, object] = {
        "state": "draining",
        "writer_handoff_count": 0,
        "writer_reacquire_count": 0,
    }
    release_entered = asyncio.Event()
    release_allowed = asyncio.Event()
    service_finish = asyncio.Event()
    handoff_zero_observed = asyncio.Event()
    depth_reads: list[str] = []

    async def service_run() -> object:
        release_entered.set()
        await release_allowed.wait()
        checkpoint["writer_handoff_count"] = 1
        await service_finish.wait()
        return SimpleNamespace(outcome="completed", reason="test")

    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    def load_checkpoint(*_args, **_kwargs) -> dict[str, object]:  # noqa: ANN002, ANN003
        snapshot = dict(checkpoint)
        if snapshot["writer_handoff_count"] == 0:
            handoff_zero_observed.set()
        return snapshot

    monkeypatch.setattr(recovery, "_load_checkpoint", load_checkpoint)

    def active_depth(*_args, **_kwargs) -> int:  # noqa: ANN002, ANN003
        depth_reads.append("read")
        return 2

    monkeypatch.setattr(recovery, "_active_exact_queue_depth", active_depth)
    service_task = asyncio.create_task(service_run())
    waiter = asyncio.create_task(
        recovery._wait_for_admission_or_post_drain(
            service_task,
            SimpleNamespace(),
            board_id=BOARD_ID,
            manifest_ref="manifest_handoff",
            timeout_seconds=1.0,
            poll_seconds=0.001,
        )
    )
    try:
        await asyncio.wait_for(release_entered.wait(), timeout=1.0)
        await asyncio.wait_for(handoff_zero_observed.wait(), timeout=1.0)
        await asyncio.sleep(0)
        assert not waiter.done()
        assert depth_reads == []

        release_allowed.set()
        observed, early_result, requires_claims = await asyncio.wait_for(
            waiter,
            timeout=1.0,
        )

        assert observed["writer_handoff_count"] == 1
        assert observed["writer_reacquire_count"] == 0
        assert early_result is None
        assert requires_claims is True
        assert depth_reads
    finally:
        release_allowed.set()
        service_finish.set()
        await asyncio.gather(service_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_drain_waiter_refuses_service_done_after_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    checkpoint = {
        "state": "draining",
        "writer_handoff_count": 1,
        "writer_reacquire_count": 0,
    }

    async def service_run() -> object:
        return SimpleNamespace(outcome="failed", reason="release failed")

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        recovery,
        "_load_checkpoint",
        lambda *_args, **_kwargs: checkpoint,
    )
    monkeypatch.setattr(
        recovery,
        "_active_exact_queue_depth",
        lambda *_args, **_kwargs: pytest.fail("queue depth read before handoff"),
    )
    service_task = asyncio.create_task(service_run())
    await service_task

    with pytest.raises(
        recovery.RecoveryRefused,
        match="rebuild_terminated_before_drain_admission",
    ):
        await recovery._wait_for_admission_or_post_drain(
            service_task,
            SimpleNamespace(),
            board_id=BOARD_ID,
            manifest_ref="manifest_handoff",
            timeout_seconds=1.0,
            poll_seconds=0.001,
        )


@pytest.mark.asyncio
async def test_drain_waiter_propagates_service_cancellation_after_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    checkpoint = {
        "state": "draining",
        "writer_handoff_count": 1,
        "writer_reacquire_count": 0,
    }

    async def service_run() -> None:
        await asyncio.Event().wait()

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        recovery,
        "_load_checkpoint",
        lambda *_args, **_kwargs: checkpoint,
    )
    monkeypatch.setattr(
        recovery,
        "_active_exact_queue_depth",
        lambda *_args, **_kwargs: pytest.fail("queue depth read before handoff"),
    )
    service_task = asyncio.create_task(service_run())
    service_task.cancel()
    await asyncio.gather(service_task, return_exceptions=True)

    with pytest.raises(asyncio.CancelledError):
        await recovery._wait_for_admission_or_post_drain(
            service_task,
            SimpleNamespace(),
            board_id=BOARD_ID,
            manifest_ref="manifest_handoff",
            timeout_seconds=1.0,
            poll_seconds=0.001,
        )


@pytest.mark.asyncio
async def test_drain_waiter_times_out_while_handoff_is_uncommitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    checkpoint = {
        "state": "draining",
        "writer_handoff_count": 0,
        "writer_reacquire_count": 0,
    }

    async def service_run() -> None:
        await asyncio.Event().wait()

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        recovery,
        "_load_checkpoint",
        lambda *_args, **_kwargs: checkpoint,
    )
    monkeypatch.setattr(
        recovery,
        "_active_exact_queue_depth",
        lambda *_args, **_kwargs: pytest.fail("queue depth read before handoff"),
    )
    service_task = asyncio.create_task(service_run())
    try:
        with pytest.raises(
            recovery.RecoveryRefused,
            match="rebuild_admission_timeout",
        ):
            await recovery._wait_for_admission_or_post_drain(
                service_task,
                SimpleNamespace(),
                board_id=BOARD_ID,
                manifest_ref="manifest_handoff",
                timeout_seconds=0.01,
                poll_seconds=0.001,
            )
    finally:
        service_task.cancel()
        await asyncio.gather(service_task, return_exceptions=True)


class _CheckpointArtifactStore:
    def __init__(self, checkpoint: Mapping[str, object] | None = None) -> None:
        self.checkpoint = checkpoint

    def read_json(self, key):  # noqa: ANN001, ANN201
        if str(getattr(key, "artifact_id", "")).startswith("f06-checkpoint-"):
            return self.checkpoint
        return None


def _closed_baseline_inputs(tmp_path: Path, *, checkpoint=None):  # noqa: ANN001
    db_path = tmp_path / "pulse.db"
    _create_queue_db(db_path)
    rebuild_root = tmp_path / "rebuild"
    quarantine_root = tmp_path / "quarantine"
    board_storage_root = tmp_path / "boards" / BOARD_ID
    rebuild_root.mkdir()
    quarantine_root.mkdir()
    board_storage_root.mkdir(parents=True)
    receipt = _receipt(state="authorized")
    audit = _terminal_audit(
        receipt,
        outcome="failed",
        reason="lifecycle_failed",
    )
    bundle = SimpleNamespace(artifact_store=_CheckpointArtifactStore(checkpoint))
    return {
        "bundle": bundle,
        "receipt": receipt,
        "audit": audit,
        "board_id": BOARD_ID,
        "db_path": db_path,
        "rebuild_root": rebuild_root,
        "rebuild_baseline": recovery._snapshot_tree_hashes(rebuild_root),
        "quarantine_root": quarantine_root,
        "quarantine_baseline": recovery._snapshot_tree_hashes(quarantine_root),
        "board_storage_root": board_storage_root,
        "raw_health": {
            "graph_state": "healthy",
            "current_kg_generation_id": None,
        },
    }


def test_closed_archive_baseline_accepts_only_zero_effect_receipt(
    tmp_path: Path,
) -> None:
    kwargs = _closed_baseline_inputs(tmp_path)

    kind, adoption = recovery._assert_closed_operation_baseline_safe(**kwargs)

    assert kind == "receipt_only"
    assert adoption is None


@pytest.mark.parametrize("partial_state", ("snapshotted", "enqueued", "draining"))
def test_closed_archive_baseline_refuses_partial_checkpoint_before_service(
    tmp_path: Path,
    partial_state: str,
) -> None:
    receipt = _receipt(state="authorized")
    checkpoint = {
        "state": partial_state,
        "command": {
            "run_id": f"f06:{receipt['manifest_ref']}",
            "board_id": BOARD_ID,
            "manifest_ref": receipt["manifest_ref"],
        },
        "receipts": {},
    }
    kwargs = _closed_baseline_inputs(tmp_path, checkpoint=checkpoint)

    with pytest.raises(
        recovery.RecoveryRefused,
        match="terminal_reconciliation_required",
    ):
        recovery._assert_closed_operation_baseline_safe(**kwargs)


def test_closed_archive_baseline_refuses_enqueued_row_without_checkpoint(
    tmp_path: Path,
) -> None:
    import sqlite3

    kwargs = _closed_baseline_inputs(tmp_path)
    receipt = kwargs["receipt"]
    with sqlite3.connect(kwargs["db_path"]) as connection:
        connection.execute(
            "INSERT INTO consolidation_queue "
            "(id, board_id, artifact_type, artifact_id, work_kind, priority, "
            "source, status, triggered_at, attempts) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "queue-partial",
                BOARD_ID,
                "spec",
                "source-1",
                "consolidate",
                1,
                f"rebuild:{receipt['manifest_ref']}",
                "pending",
                "2026-08-15T10:00:00+00:00",
                0,
            ),
        )

    with pytest.raises(
        recovery.RecoveryRefused,
        match="closed_queue_without_checkpoint",
    ):
        recovery._assert_closed_operation_baseline_safe(**kwargs)


def test_closed_archive_baseline_accepts_only_exact_completed_compensation(
    tmp_path: Path,
) -> None:
    import sqlite3

    from okto_pulse.community.adapters.rebuild_effects import CommunityRebuildEffects

    receipt = _receipt(state="authorized")
    manifest_ref = str(receipt["manifest_ref"])
    f06_run_id = f"f06:{manifest_ref}"
    source_row = {
        "artifact_type": "spec",
        "source_ref": "spec:source-1",
        "source_version": "7",
        "content_hash": "a" * 64,
        "created_at": "2026-08-15T10:00:00+00:00",
        "id": "source-1",
    }
    actions = (
        "cancel_enqueued_sources",
        "restore_quarantine",
        "discard_candidate_generation",
    )

    def effect(name: str, *, details=None):  # noqa: ANN001, ANN202
        key = f"{f06_run_id}:{name}"
        return {
            "effect_key": key,
            "effect": name,
            "ok": True,
            "code": "compensated" if name == "compensate" else "ok",
            "details": dict(details or {}),
        }

    receipts = {
        f"{f06_run_id}:snapshot": effect("snapshot"),
        f"{f06_run_id}:quarantine": effect(
            "quarantine",
            details={"affected_files": []},
        ),
        f"{f06_run_id}:enqueue": effect("enqueue"),
        f"{f06_run_id}:compensate": effect(
            "compensate",
            details={
                "actions": list(actions),
                "queue": {
                    "active_remaining": 0,
                    "live_intents_restored": 0,
                    "pending_compensated": 1,
                    "claimed_compensated": 0,
                    "total_compensated": 1,
                },
                "quarantine_restore": {"ok": True},
                "candidate_discard": {"status": "already_absent"},
            },
        ),
    }
    checkpoint = {
        "state": "failed",
        "compensation_failed_state": "enqueued",
        "compensation_actions": list(actions),
        "command": {
            "run_id": f06_run_id,
            "board_id": BOARD_ID,
            "manifest_ref": manifest_ref,
            "operation": receipt["operation"],
            "actor_id": receipt["actor_id"],
            "reason": receipt["user_reason"],
            "source_rows": [source_row],
            "previous_generation_id": None,
            "candidate_generation_id": "candidate-generation",
        },
        "receipts": receipts,
    }
    kwargs = _closed_baseline_inputs(tmp_path, checkpoint=checkpoint)
    rebuild_root = kwargs["rebuild_root"]
    assert isinstance(rebuild_root, Path)
    for effect_key, effect_payload in receipts.items():
        effect_id = CommunityRebuildEffects._effect_id(effect_key)
        _write_artifact(rebuild_root, f"audit/{effect_id}.json", effect_payload)
    kwargs["rebuild_baseline"] = recovery._snapshot_tree_hashes(rebuild_root)
    membership = {
        "run_id": manifest_ref,
        "source_ref": source_row["source_ref"],
        "source_version": source_row["source_version"],
        "content_hash": source_row["content_hash"],
    }
    with sqlite3.connect(kwargs["db_path"]) as connection:
        connection.execute(
            "INSERT INTO consolidation_queue "
            "(id, board_id, artifact_type, artifact_id, work_kind, priority, "
            "source, status, triggered_at, attempts, last_error, payload) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "queue-compensated",
                BOARD_ID,
                "spec",
                "source-1",
                "consolidate",
                1,
                f"rebuild:{manifest_ref}",
                "failed",
                "2026-08-15T10:00:00+00:00",
                0,
                "rebuild_compensated",
                json.dumps(
                    {"_rebuild_membership": membership},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )

    kind, adoption = recovery._assert_closed_operation_baseline_safe(**kwargs)

    assert kind == "fully_compensated"
    assert adoption is not None
    assert adoption.identities == frozenset({("spec", "source-1")})

    board_storage_root = kwargs["board_storage_root"]
    assert isinstance(board_storage_root, Path)
    (board_storage_root / "graph.lbug").write_bytes(b"unproven candidate bytes")
    with pytest.raises(
        recovery.RecoveryRefused,
        match="closed_compensation_graph_mismatch",
    ):
        recovery._assert_closed_operation_baseline_safe(**kwargs)


def test_bounded_recovery_lanes_use_distinct_reconciliation_and_fresh_calls() -> None:
    calls: list[bool] = []
    boundaries: list[dict[str, object]] = []
    reconciled = {
        "_recovery_phase": "reconciled",
        "reconciliation_kind": "archive_closed",
        "reconciled_run_id": "run_old",
    }
    terminal = {"run_id": "run_fresh", "publishable_status": "completed"}

    def run_lane(require_fresh: bool) -> dict[str, object]:
        calls.append(require_fresh)
        return reconciled if not require_fresh else terminal

    result = recovery._run_bounded_recovery_lanes(
        run_lane,
        validate_reconciliation_boundary=lambda value: boundaries.append(dict(value)),
    )

    assert result == terminal
    assert calls == [False, True]
    assert boundaries == [reconciled]


@pytest.mark.parametrize(
    "reconciliation_kind",
    ("archive_closed", "manifest_compensation"),
)
def test_bounded_lanes_close_capability_and_composition_before_fresh_rotation(
    reconciliation_kind: str,
) -> None:
    events: list[str] = []
    active_composition: str | None = None
    active_capability: str | None = None
    active_receipt = {"run_id": "run_old", "receipt_state": "authorized"}
    histories: dict[str, str] = {}

    def run_lane(require_fresh: bool) -> dict[str, object]:
        nonlocal active_composition, active_capability, active_receipt
        assert active_composition is None
        assert active_capability is None
        lane = "fresh" if require_fresh else "archive"
        active_composition = f"composition-{lane}"
        active_capability = f"capability-{lane}"
        events.extend((f"open:{active_composition}", f"issue:{active_capability}"))
        try:
            if not require_fresh:
                assert active_receipt == {
                    "run_id": "run_old",
                    "receipt_state": "authorized",
                }
                active_receipt = {
                    "run_id": "run_old",
                    "receipt_state": "terminal",
                }
                histories["run_old"] = "terminal"
                return {
                    "_recovery_phase": "reconciled",
                    "reconciliation_kind": reconciliation_kind,
                    "reconciled_run_id": "run_old",
                }
            assert active_receipt["receipt_state"] == "terminal"
            assert histories == {"run_old": "terminal"}
            active_receipt = {
                "run_id": "run_fresh",
                "receipt_state": "terminal",
            }
            histories["run_fresh"] = "terminal"
            return {"run_id": "run_fresh", "publishable_status": "completed"}
        finally:
            events.extend(
                (f"revoke:{active_capability}", f"close:{active_composition}")
            )
            active_capability = None
            active_composition = None

    result = recovery._run_bounded_recovery_lanes(
        run_lane,
        validate_reconciliation_boundary=lambda _value: events.append("boundary"),
    )

    assert result["run_id"] == "run_fresh"
    assert histories == {"run_old": "terminal", "run_fresh": "terminal"}
    assert events == [
        "open:composition-archive",
        "issue:capability-archive",
        "revoke:capability-archive",
        "close:composition-archive",
        "boundary",
        "open:composition-fresh",
        "issue:capability-fresh",
        "revoke:capability-fresh",
        "close:composition-fresh",
    ]


def test_crash_between_reconciliation_and_fresh_restarts_from_terminal_marker() -> None:
    active_receipt = {"run_id": "run_old", "receipt_state": "authorized"}
    histories: dict[str, str] = {}
    first_invocation_calls: list[bool] = []

    def crash_between_lanes(require_fresh: bool) -> dict[str, object]:
        nonlocal active_receipt
        first_invocation_calls.append(require_fresh)
        if require_fresh:
            raise RuntimeError("simulated_crash_between_lanes")
        active_receipt = {"run_id": "run_old", "receipt_state": "terminal"}
        histories["run_old"] = "terminal"
        return {
            "_recovery_phase": "reconciled",
            "reconciliation_kind": "manifest_compensation",
            "reconciled_run_id": "run_old",
        }

    with pytest.raises(RuntimeError, match="simulated_crash_between_lanes"):
        recovery._run_bounded_recovery_lanes(
            crash_between_lanes,
            validate_reconciliation_boundary=lambda _value: None,
        )

    assert first_invocation_calls == [False, True]
    assert active_receipt["receipt_state"] == "terminal"
    assert histories == {"run_old": "terminal"}

    second_invocation_calls: list[bool] = []

    def restart_from_terminal(require_fresh: bool) -> dict[str, object]:
        nonlocal active_receipt
        second_invocation_calls.append(require_fresh)
        assert require_fresh is False
        assert active_receipt == {
            "run_id": "run_old",
            "receipt_state": "terminal",
        }
        active_receipt = {"run_id": "run_fresh", "receipt_state": "terminal"}
        histories["run_fresh"] = "terminal"
        return {"run_id": "run_fresh", "publishable_status": "completed"}

    terminal = recovery._run_bounded_recovery_lanes(
        restart_from_terminal,
        validate_reconciliation_boundary=lambda _value: pytest.fail(
            "terminal marker incorrectly selected a second reconciliation"
        ),
    )

    assert terminal["run_id"] == "run_fresh"
    assert second_invocation_calls == [False]
    assert histories == {"run_old": "terminal", "run_fresh": "terminal"}


def test_terminal_marker_defers_fresh_manifest_until_physical_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.core.kg import rebuild_service

    terminal_receipt = _receipt(state="terminal")
    audit = _terminal_audit(
        terminal_receipt,
        outcome="failed",
        reason="lifecycle_failed",
    )
    monkeypatch.setattr(
        recovery,
        "_discover_incomplete_receipt",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        rebuild_service,
        "load_verified_rebuild_confirmation_receipt",
        lambda **_kwargs: terminal_receipt,
    )
    monkeypatch.setattr(
        recovery,
        "_create_preflight_and_manifest",
        lambda *_args, **_kwargs: pytest.fail(
            "fresh manifest built before prior-terminal physical proof"
        ),
    )
    bundle = SimpleNamespace(artifact_store=_ReceiptArtifactStore(audit))

    plan = recovery._select_recovery_run_plan(
        bundle,
        board_id=BOARD_ID,
        actor_id="owner-1",
        raw_health={},
    )

    assert plan.mode == "fresh_pending_terminal_proof"
    assert plan.manifest is None
    assert plan.previous_terminal_receipt == terminal_receipt
    assert plan.previous_terminal_audit == audit


def test_bounded_recovery_lanes_refuse_a_second_reconciliation() -> None:
    with pytest.raises(
        recovery.RecoveryRefused,
        match="reconciliation_lane_budget_exhausted",
    ):
        recovery._run_bounded_recovery_lanes(
            lambda _require_fresh: {"_recovery_phase": "reconciled"},
            validate_reconciliation_boundary=lambda _value: None,
        )


@pytest.mark.asyncio
async def test_execute_under_lock_runs_archive_lane_with_exact_gate_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import nullcontext

    from okto_pulse.community import main as community_main
    from okto_pulse.community.adapters import (
        kg_events,
        sqlalchemy_database,
        sqlalchemy_runtime_settings_service,
    )
    from okto_pulse.core import composition as core_composition

    data_home = tmp_path / "copy"
    db_path = data_home / "data" / "pulse.db"
    db_path.parent.mkdir(parents=True)
    db_path.write_bytes(b"test-only-db-placeholder")
    (data_home / "rebuild").mkdir()
    (data_home / "quarantine").mkdir()
    (data_home / "boards" / BOARD_ID).mkdir(parents=True)
    receipt = _receipt(state="authorized")
    audit = _terminal_audit(
        receipt,
        outcome="failed",
        reason="lifecycle_failed",
    )
    plan = recovery.RecoveryRunPlan(
        mode="archive_closed",
        manifest=recovery.AuthorizedManifestBinding(
            board_id=BOARD_ID,
            manifest_ref=str(receipt["manifest_ref"]),
            source_set_hash=str(receipt["source_set_hash"]),
        ),
        source_set=None,
        preflight_hash=str(receipt["preflight_hash"]),
        run_id=str(receipt["run_id"]),
        confirmation_ref=str(receipt["confirmation_ref"]),
        user_reason=str(receipt["user_reason"]),
        receipt=receipt,
        terminal_audit=audit,
    )

    class Scheduler:
        _scheduler = None

        @staticmethod
        def is_available() -> bool:
            return False

    class Workers:
        active_families = ()
        families = ()

        @staticmethod
        def start_count(_family) -> int:  # noqa: ANN001
            return 0

    composition = SimpleNamespace(
        scheduler_control=Scheduler(),
        worker_registry=Workers(),
    )

    class Transaction:
        _rolled_back = False

        def rollback(self) -> None:
            self._rolled_back = True

    transaction = Transaction()
    app = SimpleNamespace(
        state=SimpleNamespace(
            runtime_composition=composition,
            mcp_cold_start_transaction=transaction,
        )
    )
    lock = SimpleNamespace(inspect=lambda **_kwargs: None)
    service_calls: list[dict[str, object]] = []

    def resume_authorized_run(**kwargs):  # noqa: ANN003, ANN202
        service_calls.append(dict(kwargs))
        return SimpleNamespace(run_id=receipt["run_id"])

    bundle = SimpleNamespace(
        single_writer_lock=lock,
        operation_reservation=lock,
        service=SimpleNamespace(resume_authorized_run=resume_authorized_run),
        event_manifest_bindings={},
    )
    queue = recovery.QueueSnapshot((), (), "queue-fingerprint")
    gate_calls: list[dict[str, object]] = []

    async def archive_gate(service_task, *_args, **kwargs):  # noqa: ANN001, ANN003
        gate_calls.append(dict(kwargs))
        await service_task
        return {
            "_recovery_phase": "reconciled",
            "reconciliation_kind": "archive_closed",
            "reconciled_run_id": receipt["run_id"],
        }

    async def noop_async(*_args, **_kwargs):  # noqa: ANN202
        return None

    monkeypatch.setattr(community_main, "create_community_app", lambda: app)
    monkeypatch.setattr(sqlalchemy_database, "get_session_factory", lambda: object())
    monkeypatch.setattr(
        sqlalchemy_runtime_settings_service,
        "apply_persisted_settings_to_core_settings",
        noop_async,
    )
    monkeypatch.setattr(
        kg_events, "register_community_kg_events_reader", lambda *_: None
    )
    monkeypatch.setattr(
        core_composition,
        "runtime_composition_scope",
        lambda _composition: nullcontext(),
    )
    monkeypatch.setattr(recovery, "_assert_full_orm_schema_present", lambda *_: None)
    monkeypatch.setattr(recovery, "_assert_schema_unchanged", lambda *_: None)
    monkeypatch.setattr(
        recovery, "_assert_worker_registry_never_started", lambda *_: None
    )
    monkeypatch.setattr(recovery, "_assert_no_pulse_processes", lambda: None)
    monkeypatch.setattr(recovery, "_port_is_listening", lambda _port: False)
    monkeypatch.setattr(
        recovery,
        "OfflineProcessProbe",
        lambda: SimpleNamespace(is_offline=lambda: True),
    )
    monkeypatch.setattr(
        recovery,
        "_authoritative_serve_lock_matches",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(recovery, "_authorize_governed_rebuild", noop_async)
    monkeypatch.setattr(
        recovery,
        "_offline_cold_graph_health",
        lambda *_args, **_kwargs: {
            "graph_state": "recovery_needed",
            "metric_status": "unavailable",
            "current_kg_generation_id": None,
            "graph_storage_exists": False,
            "graph_storage_locked": False,
        },
    )
    monkeypatch.setattr(
        recovery,
        "_build_service_bundle",
        lambda **_kwargs: bundle,
    )
    monkeypatch.setattr(
        recovery,
        "_discover_legacy_queue_only_reconciliation",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(recovery, "_select_recovery_run_plan", lambda *_a, **_k: plan)
    monkeypatch.setattr(
        recovery,
        "_assert_closed_operation_baseline_safe",
        lambda *_args, **_kwargs: ("receipt_only", None),
    )
    monkeypatch.setattr(
        recovery,
        "_capture_cognitive_ledger_baseline",
        lambda **_kwargs: recovery.CognitiveLedgerBaseline(records=()),
    )
    monkeypatch.setattr(recovery, "_protected_queue_snapshot", lambda *_a, **_k: queue)
    monkeypatch.setattr(recovery, "_dlq_snapshot", lambda *_a, **_k: queue)
    monkeypatch.setattr(recovery, "_canonical_debt_snapshot", lambda *_a, **_k: queue)
    monkeypatch.setattr(recovery, "_dlq_ids_for_board", lambda *_a, **_k: ())
    monkeypatch.setattr(recovery, "_sqlite_logical_fingerprints", lambda *_a, **_k: {})
    monkeypatch.setattr(
        recovery,
        "_recovery_capability_context",
        lambda **_kwargs: nullcontext(object()),
    )
    monkeypatch.setattr(recovery, "_await_closed_archive_reconciliation", archive_gate)
    monkeypatch.setattr(recovery, "_shutdown_composed_runtime", noop_async)
    monkeypatch.setattr(recovery, "_emit", lambda *_args, **_kwargs: None)

    result = await recovery._execute_under_serve_lock(
        SimpleNamespace(
            board_id=BOARD_ID,
            offline_ports=(),
            run_timeout_seconds=1.0,
            admission_timeout_seconds=1.0,
            poll_seconds=0.02,
            batch_size=1,
            reason="test archive lane",
        ),
        data_home=data_home,
        db_path=db_path,
        owner_id="owner-1",
        schema_fingerprint="schema-test",
        serve_lock=object(),
        heartbeat=SimpleNamespace(failure=None),
        require_fresh=False,
    )

    assert result["reconciliation_kind"] == "archive_closed"
    assert len(service_calls) == 1
    assert len(gate_calls) == 1
    assert "expected_board_storage" not in gate_calls[0]
    assert transaction._rolled_back


def test_service_thread_finishes_before_revocation_and_capability_close() -> None:
    source = inspect.getsource(recovery._execute_under_serve_lock)
    revoke_offset = source.index("revoke_unconsumed(")
    fenced_wait_offset = source.rfind(
        "await _await_service_fenced(",
        0,
        revoke_offset,
    )
    capability_close_offset = source.index("capability_stack.close()", revoke_offset)
    exact_recovery_offset = source.index("await _recover_exact_claims_for_resume(")
    exact_drain_offset = source.index("drain_outcome = await _drain_exact_scope(")

    assert 0 <= fenced_wait_offset < revoke_offset < capability_close_offset
    assert exact_recovery_offset < exact_drain_offset


@pytest.mark.asyncio
async def test_exact_claim_replay_is_durable_idempotent_and_scope_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model the graph-commit-before-ACK crash cut with two durable stores."""

    import sqlite3

    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    from okto_pulse.community.adapters.sqlalchemy_base import Base
    from okto_pulse.community.adapters.sqlalchemy_consolidation import (
        CommunitySqlAlchemyConsolidationPersistence,
    )
    from okto_pulse.community.adapters.sqlalchemy_models import (
        Board,
        ConsolidationQueue,
    )
    from okto_pulse.core.application.processors import consolidation
    from okto_pulse.core.ports.consolidation import (
        ConsolidationClaimScope,
        register_consolidation_persistence_port,
        reset_consolidation_persistence_port_for_tests,
    )

    queue_db = tmp_path / "queue.db"
    durable_graph_db = tmp_path / "durable-graph-test-adapter.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{queue_db.as_posix()}")

    @event.listens_for(engine.sync_engine, "connect")
    def configure_sqlite(dbapi_connection, _connection_record):  # noqa: ANN001, ANN202
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    target_source = "rebuild:manifest-durable-crash-cut"
    unrelated_board = "00000000-0000-0000-0000-000000000002"
    now = datetime.now(timezone.utc)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session:
        session.add_all(
            (
                Board(id=BOARD_ID, name="Target", owner_id="owner-1"),
                Board(id=unrelated_board, name="Unrelated", owner_id="owner-2"),
                ConsolidationQueue(
                    id="target-graph-committed-before-ack",
                    board_id=BOARD_ID,
                    artifact_type="spec",
                    artifact_id="durable-spec",
                    work_kind="consolidate",
                    generation=0,
                    priority="high",
                    source=target_source,
                    status="claimed",
                    triggered_at=now,
                    claimed_at=now,
                    claim_timeout_at=now + timedelta(minutes=10),
                    worker_id="killed-worker",
                    claimed_by_session_id="killed-worker",
                    claim_token="killed-claim-token",
                ),
                ConsolidationQueue(
                    id="unrelated-live-claim",
                    board_id=unrelated_board,
                    artifact_type="spec",
                    artifact_id="unrelated-spec",
                    work_kind="consolidate",
                    generation=0,
                    priority="high",
                    source="event:spec.updated",
                    status="claimed",
                    triggered_at=now,
                    claimed_at=now,
                    claim_timeout_at=now + timedelta(minutes=10),
                    worker_id="live-worker",
                    claimed_by_session_id="live-worker",
                    claim_token="live-claim-token",
                ),
            )
        )
        await session.commit()

    # This commit represents the native graph effect that survived while the
    # worker died before compare-and-delete ACK of its claimed queue row.
    with sqlite3.connect(durable_graph_db) as graph:
        graph.execute("CREATE TABLE graph_nodes (identity TEXT PRIMARY KEY)")
        graph.execute(
            "CREATE TABLE replay_audit "
            "(sequence INTEGER PRIMARY KEY AUTOINCREMENT, identity TEXT NOT NULL)"
        )
        graph.execute(
            "INSERT INTO graph_nodes(identity) VALUES (?)",
            ("spec:durable-spec",),
        )
        graph.commit()

    async def reservation_source(_db, *, board_id: str):  # noqa: ANN001, ANN202
        return target_source if board_id == BOARD_ID else None

    async def durable_idempotent_graph_adapter(
        _db,
        entry,
        **_kwargs,
    ):  # noqa: ANN001, ANN003, ANN202
        identity = f"{entry.artifact_type}:{entry.artifact_id}"
        with sqlite3.connect(durable_graph_db) as graph:
            graph.execute(
                "INSERT OR IGNORE INTO graph_nodes(identity) VALUES (?)",
                (identity,),
            )
            graph.execute(
                "INSERT INTO replay_audit(identity) VALUES (?)",
                (identity,),
            )
            graph.commit()
        return True

    adapter = CommunitySqlAlchemyConsolidationPersistence()
    monkeypatch.setattr(
        adapter,
        "board_administrative_rebuild_source",
        reservation_source,
    )
    monkeypatch.setattr(
        consolidation,
        "_process_queue_entry_serialized",
        durable_idempotent_graph_adapter,
    )
    register_consolidation_persistence_port(adapter)
    try:
        processor = consolidation.ConsolidationProcessor(factory, batch_size=1)
        scope = ConsolidationClaimScope(
            board_id=BOARD_ID,
            source=target_source,
            reservation_lineage_id="b" * 64,
        )
        assert (
            await processor.recover_exact_claims(
                claim_scope=scope,
                recovery_authority_probe=lambda: True,
            )
            == 1
        )
        async with factory() as session:
            recovered = await session.get(
                ConsolidationQueue,
                "target-graph-committed-before-ack",
            )
            unrelated = await session.get(
                ConsolidationQueue,
                "unrelated-live-claim",
            )
            assert recovered is not None
            assert recovered.status == "pending"
            assert recovered.claim_token is None
            assert unrelated is not None
            assert unrelated.status == "claimed"
            assert unrelated.claim_token == "live-claim-token"

        assert await processor.process_batch(claim_scope=scope) == 1
    finally:
        reset_consolidation_persistence_port_for_tests()

    # Reopen the independent durable effect store: replay ran once, the
    # logical graph identity remains unique, and no in-memory set is involved.
    with sqlite3.connect(durable_graph_db) as graph:
        assert graph.execute(
            "SELECT COUNT(*) FROM graph_nodes WHERE identity=?",
            ("spec:durable-spec",),
        ).fetchone() == (1,)
        assert graph.execute(
            "SELECT COUNT(*) FROM replay_audit WHERE identity=?",
            ("spec:durable-spec",),
        ).fetchone() == (1,)
    async with factory() as session:
        assert (
            await session.get(
                ConsolidationQueue,
                "target-graph-committed-before-ack",
            )
            is None
        )
        unrelated = await session.get(ConsolidationQueue, "unrelated-live-claim")
        assert unrelated is not None
        assert unrelated.status == "claimed"
        assert unrelated.claim_token == "live-claim-token"
    await engine.dispose()


def _write_artifact(root: Path, relative: str | Path, payload: object) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _cognitive_item(
    *,
    generation_id: str,
    source_ref: str,
    status: str,
    content_hash: str | None,
    recorded_at: str,
    event_ref: str,
    artifact_type: str = "spec",
) -> dict[str, object]:
    from okto_pulse.core.kg.rebuild_audit import (
        CognitiveConsolidationItem,
        compute_cognitive_item_id,
    )

    return CognitiveConsolidationItem(
        item_id=compute_cognitive_item_id(BOARD_ID, generation_id, source_ref),
        board_id=BOARD_ID,
        kg_generation_id=generation_id,
        source_ref=source_ref,
        artifact_type=artifact_type,
        status=status,
        recorded_at=recorded_at,
        event_ref=event_ref,
        content_hash=content_hash,
    ).to_dict()


def _terminal_artifact_fixture(
    tmp_path: Path,
    *,
    resume_mode: bool,
    include_confirmation_audit: bool = True,
    prior_terminal_active: bool = False,
    baseline_cognitive_records: tuple[dict[str, object], ...] = (),
) -> tuple[Path, dict[str, str], dict[str, object]]:
    from okto_pulse.community.adapters.rebuild_effects import CommunityRebuildEffects

    rebuild_root = tmp_path / "rebuild"
    run_id = "run_0123456789abcdef01234567"
    manifest_ref = "manifest_authorized"
    generation_id = "generation_terminal"
    report_id = "report_terminal"
    report_ref = "report://terminal"
    preflight_hash = "a" * 64
    confirmation_ref = f"conf_fp_{'c' * 64}"
    active_relative = f"audit/confirmation_receipts/{BOARD_ID}/active.json"
    old_run_id = "run_aaaaaaaaaaaaaaaaaaaaaaaa"
    if prior_terminal_active:
        old_terminal = {
            "run_id": old_run_id,
            "board_id": BOARD_ID,
            "manifest_ref": "manifest_previous",
            "receipt_state": "terminal",
        }
        _write_artifact(rebuild_root, active_relative, old_terminal)
        _write_artifact(
            rebuild_root,
            f"audit/confirmation_receipts/{BOARD_ID}/{old_run_id}.json",
            old_terminal,
        )
    else:
        _write_artifact(
            rebuild_root,
            active_relative,
            {
                "run_id": run_id,
                "board_id": BOARD_ID,
                "manifest_ref": manifest_ref,
                "receipt_state": "authorized",
            },
        )
    overlay_baseline = {
        "version": 1,
        "state": "stable",
        "revision": 7,
        "nonce": "baseline-overlay-nonce-0001",
    }
    _write_artifact(
        rebuild_root,
        recovery._COGNITIVE_OVERLAY_REVISION_RELATIVE,
        overlay_baseline,
    )
    if resume_mode:
        _write_artifact(
            rebuild_root,
            f"manifests/{manifest_ref}.json",
            {"manifest_ref": manifest_ref, "run_id": run_id},
        )
    confirmation_payload = {
        "audit_id": f"audit_{'d' * 32}",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "board_id": BOARD_ID,
        "operation": "rebuild",
        "outcome": "consumed",
        "reason": "n/a",
        "actor_ref": "actor:human",
        "preflight_hash": preflight_hash,
        "generation_ids": {"manifest_ref": manifest_ref},
        "affected_files": [],
        "confirmation_ref": confirmation_ref,
    }
    if resume_mode and include_confirmation_audit:
        _write_artifact(
            rebuild_root,
            f"audit/confirmation/{BOARD_ID}/audit_{'d' * 32}.json",
            confirmation_payload,
        )
    for record in baseline_cognitive_records:
        generation = str(record["kg_generation_id"])
        _write_artifact(
            rebuild_root,
            f"audit/cognitive_pending/{BOARD_ID}/{generation}.json",
            record,
        )
    baseline = recovery._snapshot_tree_hashes(rebuild_root)
    cognitive_ledger_baseline = recovery._capture_cognitive_ledger_baseline(
        rebuild_root=rebuild_root,
        rebuild_baseline=baseline,
        board_id=BOARD_ID,
    )

    terminal_receipt = {
        "run_id": run_id,
        "board_id": BOARD_ID,
        "manifest_ref": manifest_ref,
        "receipt_state": "terminal",
    }
    _write_artifact(rebuild_root, active_relative, terminal_receipt)
    history_path = _write_artifact(
        rebuild_root,
        f"audit/confirmation_receipts/{BOARD_ID}/{run_id}.json",
        terminal_receipt,
    )
    _write_artifact(
        rebuild_root,
        recovery._COGNITIVE_OVERLAY_REVISION_RELATIVE,
        {
            "version": 1,
            "state": "stable",
            "revision": 8,
            "nonce": "terminal-overlay-nonce-0002",
        },
    )
    _write_artifact(
        rebuild_root,
        f"manifests/{manifest_ref}.json",
        {"manifest_ref": manifest_ref, "run_id": run_id},
    )
    _write_artifact(
        rebuild_root,
        f"reports/{report_id}.json",
        {"report_id": report_id, "report_ref": report_ref, "run_id": run_id},
    )
    _write_artifact(
        rebuild_root,
        f"generations/{BOARD_ID}/current.json",
        {"kg_generation_id": generation_id, "run_id": run_id},
    )
    _write_artifact(
        rebuild_root,
        f"generations/{BOARD_ID}/history/{generation_id}.json",
        {"kg_generation_id": generation_id, "run_id": run_id},
    )
    _write_artifact(
        rebuild_root,
        f"audit/{run_id}.json",
        {"run_id": run_id, "manifest_ref": manifest_ref},
    )
    f06_run_id = f"f06:{manifest_ref}"
    f06_ids = {
        CommunityRebuildEffects._checkpoint_id(f06_run_id),
        *(
            CommunityRebuildEffects._effect_id(f"{f06_run_id}:{effect}")
            for effect in ("snapshot", "quarantine", "enqueue", "restore", "promote")
        ),
        CommunityRebuildEffects._effect_id(f"{f06_run_id}:audit:completed"),
    }
    for artifact_id in f06_ids:
        _write_artifact(
            rebuild_root,
            f"audit/{artifact_id}.json",
            {"run_id": f06_run_id, "manifest_ref": manifest_ref},
        )
    event_id = (
        "evt_"
        + __import__("hashlib")
        .sha256(f"{BOARD_ID}\x1f{run_id}".encode())
        .hexdigest()[:32]
    )
    _write_artifact(
        rebuild_root,
        f"audit/events/{BOARD_ID}/{event_id}.json",
        {
            "event_id": event_id,
            "event": "kg.rebuilt",
            "board_id": BOARD_ID,
            "run_id": run_id,
            "manifest_ref": manifest_ref,
            "report_ref": report_ref,
            "kg_generation_id": generation_id,
            "status": "completed",
            "delivery_outcome": "published",
        },
    )
    _write_artifact(
        rebuild_root,
        f"audit/cognitive_pending/{BOARD_ID}/{generation_id}.json",
        {
            "board_id": BOARD_ID,
            "kg_generation_id": generation_id,
            "event_ref": event_id,
            "pending_count": 0,
            "pending_refs": [],
            "status": "skipped",
            "recorded_at": "2026-08-15T10:00:00+00:00",
            "items": [],
        },
    )
    if not resume_mode and include_confirmation_audit:
        _write_artifact(
            rebuild_root,
            f"audit/confirmation/{BOARD_ID}/audit_{'d' * 32}.json",
            confirmation_payload,
        )

    kwargs: dict[str, object] = {
        "rebuild_root": rebuild_root,
        "baseline": baseline,
        "board_id": BOARD_ID,
        "actor_id": "owner-1",
        "manifest_ref": manifest_ref,
        "run_id": run_id,
        "generation_id": generation_id,
        "report_id": report_id,
        "report_ref": report_ref,
        "preflight_hash": preflight_hash,
        "confirmation_ref": confirmation_ref,
        "confirmation_receipt_ref": str(history_path),
        "resume_mode": resume_mode,
        "overlay_revision_baseline": overlay_baseline,
        "expect_overlay_revision_advance": True,
        "expected_cognitive_source_rows": (),
        "cognitive_ledger_baseline": cognitive_ledger_baseline,
        "frozen_confirmation_receipt_baseline": None,
    }
    return rebuild_root, baseline, kwargs


def _stage_terminal_rebaseline_evidence(
    rebuild_root: Path,
    kwargs: dict[str, object],
    *,
    target_hash: str,
) -> dict[str, object]:
    run_id = str(kwargs["run_id"])
    manifest_ref = str(kwargs["manifest_ref"])
    event_id = (
        "evt_"
        + __import__("hashlib")
        .sha256(f"{BOARD_ID}\x1f{run_id}".encode())
        .hexdigest()[:32]
    )
    event_path = rebuild_root / "audit" / "events" / BOARD_ID / f"{event_id}.json"
    event_payload = dict(
        recovery._load_json_file(event_path, "test_rebaseline_event_invalid")
    )
    evidence_id = f"{run_id}:{manifest_ref}"
    event_payload.update(
        {
            "rebaseline_evidence_id": evidence_id,
            "rebaseline_target_source_set_hash": target_hash,
        }
    )
    _write_artifact(rebuild_root, event_path.relative_to(rebuild_root), event_payload)
    evidence: dict[str, object] = {
        "outcome": "rebaseline",
        "from_source_set_hash": "1" * 64,
        "to_source_set_hash": target_hash,
        "rebaselined_source_refs": ["spec:legacy"],
    }
    recorded_at = "2026-08-15T09:59:00+00:00"
    audit_payload: dict[str, object] = {
        "board_id": BOARD_ID,
        "artifact_id": "records",
        "updated_at": recorded_at,
        "records": [
            {
                "board_id": BOARD_ID,
                "manifest_ref": manifest_ref,
                "evidence_id": evidence_id,
                **evidence,
                "recorded_at": recorded_at,
            }
        ],
    }
    _write_artifact(
        rebuild_root,
        recovery._REBASELINE_AUDIT_RELATIVE,
        audit_payload,
    )
    kwargs["expected_rebaseline_evidence"] = evidence
    return audit_payload


def _stage_frozen_receipt_archive_crash_cut(
    rebuild_root: Path,
    kwargs: dict[str, object],
) -> tuple[
    dict[str, str],
    dict[str, object],
    recovery.CognitiveLedgerBaseline,
    dict[str, object],
]:
    active_path = (
        rebuild_root / "audit" / "confirmation_receipts" / BOARD_ID / "active.json"
    )
    history_path = Path(str(kwargs["confirmation_receipt_ref"]))
    terminal = dict(
        recovery._load_json_file(active_path, "test_terminal_receipt_invalid")
    )
    authorized = {**terminal, "receipt_state": "authorized"}
    _write_artifact(rebuild_root, active_path.relative_to(rebuild_root), authorized)
    history_path.unlink(missing_ok=True)
    frozen_baseline = recovery._snapshot_tree_hashes(rebuild_root)
    frozen_overlay = dict(
        recovery._load_json_file(
            rebuild_root / recovery._COGNITIVE_OVERLAY_REVISION_RELATIVE,
            "test_frozen_overlay",
        )
    )
    cognitive_baseline = recovery._capture_cognitive_ledger_baseline(
        rebuild_root=rebuild_root,
        rebuild_baseline=frozen_baseline,
        board_id=BOARD_ID,
    )
    archived = {**authorized, "receipt_state": "terminal"}
    _write_artifact(rebuild_root, active_path.relative_to(rebuild_root), archived)
    _write_artifact(rebuild_root, history_path.relative_to(rebuild_root), archived)
    return frozen_baseline, frozen_overlay, cognitive_baseline, authorized


@pytest.mark.parametrize("include_confirmation_audit", (False, True))
def test_resume_artifact_gate_allows_exact_terminal_family_and_audit_crash_cut(
    tmp_path: Path,
    include_confirmation_audit: bool,
) -> None:
    _root, _baseline, kwargs = _terminal_artifact_fixture(
        tmp_path,
        resume_mode=True,
        include_confirmation_audit=include_confirmation_audit,
    )

    recovery._assert_rebuild_artifacts_governed(**kwargs)


def test_rebaseline_terminal_event_is_bound_to_run_evidence_and_target(
    tmp_path: Path,
) -> None:
    rebuild_root, _baseline, kwargs = _terminal_artifact_fixture(
        tmp_path,
        resume_mode=True,
    )
    _stage_terminal_rebaseline_evidence(
        rebuild_root,
        kwargs,
        target_hash="2" * 64,
    )

    recovery._assert_rebuild_artifacts_governed(**kwargs)

    run_id = str(kwargs["run_id"])
    event_id = (
        "evt_"
        + __import__("hashlib")
        .sha256(f"{BOARD_ID}\x1f{run_id}".encode())
        .hexdigest()[:32]
    )
    event_relative = f"audit/events/{BOARD_ID}/{event_id}.json"
    payload = dict(
        recovery._load_json_file(
            rebuild_root / event_relative,
            "test_rebaseline_event_invalid",
        )
    )
    payload["rebaseline_target_source_set_hash"] = "3" * 64
    _write_artifact(rebuild_root, event_relative, payload)
    with pytest.raises(
        recovery.RecoveryRefused,
        match="terminal_rebuild_event_rebaseline_binding_invalid",
    ):
        recovery._assert_rebuild_artifacts_governed(**kwargs)


def test_frozen_legacy_rebaseline_uses_captured_v3_ledger_as_authority(
    tmp_path: Path,
) -> None:
    rebuild_root, _baseline, kwargs = _terminal_artifact_fixture(
        tmp_path,
        resume_mode=True,
    )
    target_hash = "2" * 64
    rebaseline_audit = _stage_terminal_rebaseline_evidence(
        rebuild_root,
        kwargs,
        target_hash=target_hash,
    )
    run_id = str(kwargs["run_id"])
    generation_id = str(kwargs["generation_id"])
    event_id = (
        "evt_"
        + __import__("hashlib")
        .sha256(f"{BOARD_ID}\x1f{run_id}".encode())
        .hexdigest()[:32]
    )
    source_ref = "spec:legacy"
    recorded_at = "2026-08-15T10:00:00+00:00"
    v3_item = _cognitive_item(
        generation_id=generation_id,
        source_ref=source_ref,
        status="pending",
        content_hash="3" * 64,
        recorded_at=recorded_at,
        event_ref=event_id,
    )
    _write_artifact(
        rebuild_root,
        f"audit/cognitive_pending/{BOARD_ID}/{generation_id}.json",
        {
            "board_id": BOARD_ID,
            "kg_generation_id": generation_id,
            "event_ref": event_id,
            "pending_count": 1,
            "pending_refs": [source_ref],
            "status": "pending_marked",
            "recorded_at": recorded_at,
            "items": [v3_item],
        },
    )
    (
        frozen_baseline,
        frozen_overlay,
        frozen_cognitive_baseline,
        authorized_receipt,
    ) = _stage_frozen_receipt_archive_crash_cut(rebuild_root, kwargs)
    kwargs.update(
        {
            "baseline": frozen_baseline,
            "overlay_revision_baseline": frozen_overlay,
            "expect_overlay_revision_advance": False,
            # The v1/v2 manifest hash is intentionally different from the
            # already-persisted v3 ledger hash. Frozen replay must preserve
            # the captured terminal ledger, not reconstruct it from legacy.
            "expected_cognitive_source_rows": (
                {
                    "source_ref": source_ref,
                    "artifact_type": "spec",
                    "content_hash": "1" * 64,
                },
            ),
            "cognitive_ledger_baseline": frozen_cognitive_baseline,
            "frozen_confirmation_receipt_baseline": authorized_receipt,
            "rebaseline_audit_baseline": rebaseline_audit,
        }
    )

    recovery._assert_rebuild_artifacts_governed(**kwargs)


def test_terminal_rebaseline_source_binding_rejects_v3_only_drift() -> None:
    from okto_pulse.core.kg.rebuild_sources import SourceSetRevalidation

    expected_target = "4" * 64
    observed_target = "5" * 64
    bundle = SimpleNamespace(
        source_enumerator=SimpleNamespace(enumerate=lambda **_kwargs: object()),
        manifest_store=SimpleNamespace(
            classify_revalidation=lambda **_kwargs: SimpleNamespace(
                outcome=SourceSetRevalidation.REBASELINE,
                to_source_set_hash=observed_target,
            )
        ),
    )

    with pytest.raises(
        recovery.RecoveryRefused,
        match="terminal_rebaseline_target_source_set_changed",
    ):
        recovery._assert_source_unchanged(
            bundle,
            object(),
            BOARD_ID,
            require_terminal_binding=True,
            expected_rebaseline_target_source_set_hash=expected_target,
        )


def test_second_fresh_artifact_gate_rotates_only_active_receipt(
    tmp_path: Path,
) -> None:
    rebuild_root, baseline, kwargs = _terminal_artifact_fixture(
        tmp_path,
        resume_mode=False,
        prior_terminal_active=True,
    )
    old_history = (
        f"audit/confirmation_receipts/{BOARD_ID}/run_aaaaaaaaaaaaaaaaaaaaaaaa.json"
    )
    old_history_hash = baseline[old_history]

    recovery._assert_rebuild_artifacts_governed(**kwargs)

    assert (
        __import__("hashlib")
        .sha256((rebuild_root / old_history).read_bytes())
        .hexdigest()
        == old_history_hash
    )


def test_frozen_terminal_artifact_gate_requires_zero_effect_replay(
    tmp_path: Path,
) -> None:
    rebuild_root, _baseline, kwargs = _terminal_artifact_fixture(
        tmp_path,
        resume_mode=True,
    )
    (
        frozen_baseline,
        frozen_overlay,
        frozen_cognitive_baseline,
        authorized_receipt,
    ) = _stage_frozen_receipt_archive_crash_cut(
        rebuild_root,
        kwargs,
    )
    kwargs.update(
        {
            "baseline": frozen_baseline,
            "overlay_revision_baseline": frozen_overlay,
            "expect_overlay_revision_advance": False,
            "cognitive_ledger_baseline": frozen_cognitive_baseline,
            "frozen_confirmation_receipt_baseline": authorized_receipt,
        }
    )

    recovery._assert_rebuild_artifacts_governed(**kwargs)

    _write_artifact(
        rebuild_root,
        f"reports/{kwargs['report_id']}.json",
        {
            "report_id": kwargs["report_id"],
            "report_ref": kwargs["report_ref"],
            "run_id": kwargs["run_id"],
            "unexpected_frozen_mutation": True,
        },
    )
    with pytest.raises(
        recovery.RecoveryRefused,
        match="frozen_terminal_rebuild_tree_changed",
    ):
        recovery._assert_rebuild_artifacts_governed(**kwargs)


def test_frozen_terminal_artifact_gate_preserves_agent_owned_status_mix(
    tmp_path: Path,
) -> None:
    rebuild_root, _baseline, kwargs = _terminal_artifact_fixture(
        tmp_path,
        resume_mode=True,
    )
    run_id = str(kwargs["run_id"])
    generation_id = str(kwargs["generation_id"])
    event_id = (
        "evt_"
        + __import__("hashlib")
        .sha256(f"{BOARD_ID}\x1f{run_id}".encode())
        .hexdigest()[:32]
    )
    recorded_at = "2026-08-15T10:00:00+00:00"
    expected_rows: list[dict[str, object]] = []
    items: list[dict[str, object]] = []
    for index, status in enumerate(("in_progress", "failed", "consolidated")):
        source_ref = f"spec:frozen-{status}"
        content_hash = f"{index + 301:064x}"
        expected_rows.append(
            {
                "id": f"frozen-{status}",
                "source_ref": source_ref,
                "artifact_type": "spec",
                "content_hash": content_hash,
            }
        )
        items.append(
            _cognitive_item(
                generation_id=generation_id,
                source_ref=source_ref,
                status=status,
                content_hash=content_hash,
                recorded_at=recorded_at,
                event_ref=event_id,
            )
        )
    cognitive_relative = f"audit/cognitive_pending/{BOARD_ID}/{generation_id}.json"
    _write_artifact(
        rebuild_root,
        cognitive_relative,
        {
            "board_id": BOARD_ID,
            "kg_generation_id": generation_id,
            "event_ref": event_id,
            "pending_count": 2,
            "pending_refs": sorted(
                str(item["source_ref"])
                for item in items
                if item["status"] in {"in_progress", "failed"}
            ),
            "status": "pending_marked",
            "recorded_at": recorded_at,
            "items": items,
        },
    )
    (
        frozen_baseline,
        frozen_overlay,
        frozen_cognitive_baseline,
        authorized_receipt,
    ) = _stage_frozen_receipt_archive_crash_cut(
        rebuild_root,
        kwargs,
    )
    kwargs.update(
        {
            "baseline": frozen_baseline,
            "overlay_revision_baseline": frozen_overlay,
            "expect_overlay_revision_advance": False,
            "expected_cognitive_source_rows": tuple(expected_rows),
            "cognitive_ledger_baseline": frozen_cognitive_baseline,
            "frozen_confirmation_receipt_baseline": authorized_receipt,
        }
    )

    recovery._assert_rebuild_artifacts_governed(**kwargs)

    payload = dict(
        recovery._load_json_file(
            rebuild_root / cognitive_relative,
            "test_frozen_status_mix",
        )
    )
    changed_items = [dict(item) for item in payload["items"]]
    changed_items[0]["status"] = "pending"
    payload["items"] = changed_items
    _write_artifact(rebuild_root, cognitive_relative, payload)
    with pytest.raises(recovery.RecoveryRefused):
        recovery._assert_rebuild_artifacts_governed(**kwargs)


def test_cognitive_terminal_ledger_is_exactly_bound_to_expected_sources(
    tmp_path: Path,
) -> None:
    from okto_pulse.core.kg.rebuild_audit import (
        CognitiveConsolidationItem,
        compute_cognitive_item_id,
    )

    rebuild_root, _baseline, kwargs = _terminal_artifact_fixture(
        tmp_path,
        resume_mode=True,
    )
    source_row = {
        "id": "durable-spec",
        "source_ref": "spec:durable-spec",
        "artifact_type": "spec",
        "content_hash": "f" * 64,
    }
    run_id = str(kwargs["run_id"])
    generation_id = str(kwargs["generation_id"])
    event_id = (
        "evt_"
        + __import__("hashlib")
        .sha256(f"{BOARD_ID}\x1f{run_id}".encode())
        .hexdigest()[:32]
    )
    cognitive_relative = f"audit/cognitive_pending/{BOARD_ID}/{generation_id}.json"
    recorded_at = "2026-08-15T10:00:00+00:00"
    item = CognitiveConsolidationItem(
        item_id=compute_cognitive_item_id(
            BOARD_ID,
            generation_id,
            str(source_row["source_ref"]),
        ),
        board_id=BOARD_ID,
        kg_generation_id=generation_id,
        source_ref=str(source_row["source_ref"]),
        artifact_type="spec",
        status="pending",
        recorded_at=recorded_at,
        event_ref=event_id,
        content_hash=str(source_row["content_hash"]),
    )
    _write_artifact(
        rebuild_root,
        cognitive_relative,
        {
            "board_id": BOARD_ID,
            "kg_generation_id": generation_id,
            "event_ref": event_id,
            "pending_count": 1,
            "pending_refs": [source_row["source_ref"]],
            "status": "pending_marked",
            "recorded_at": recorded_at,
            "items": [item.to_dict()],
        },
    )
    kwargs["expected_cognitive_source_rows"] = (source_row,)

    recovery._assert_rebuild_artifacts_governed(**kwargs)

    payload = dict(
        recovery._load_json_file(
            rebuild_root / cognitive_relative,
            "test_cognitive_payload",
        )
    )
    raw_items = list(payload["items"])
    payload["items"] = [{**dict(raw_items[0]), "source_ref": "spec:unexpected"}]
    _write_artifact(rebuild_root, cognitive_relative, payload)
    with pytest.raises(recovery.RecoveryRefused, match="cognitive_source_ref"):
        recovery._assert_rebuild_artifacts_governed(**kwargs)


def test_cognitive_baseline_capture_accepts_chained_verbatim_carry(
    tmp_path: Path,
) -> None:
    rebuild_root = tmp_path / "rebuild"
    container_generation = "generation-second"
    carried_generation = "generation-first"
    carried = _cognitive_item(
        generation_id=carried_generation,
        source_ref="spec:carried",
        status="consolidated",
        content_hash="a" * 64,
        recorded_at="2026-08-14T09:00:00+00:00",
        event_ref="evt_first",
    )
    _write_artifact(
        rebuild_root,
        f"audit/cognitive_pending/{BOARD_ID}/{container_generation}.json",
        {
            "board_id": BOARD_ID,
            "kg_generation_id": container_generation,
            "event_ref": "evt_second",
            "pending_count": 0,
            "pending_refs": [],
            "status": "pending_marked",
            "recorded_at": "2026-08-14T10:00:00+00:00",
            "items": [carried],
        },
    )
    baseline = recovery._snapshot_tree_hashes(rebuild_root)

    captured = recovery._capture_cognitive_ledger_baseline(
        rebuild_root=rebuild_root,
        rebuild_baseline=baseline,
        board_id=BOARD_ID,
    )

    assert len(captured.records) == 1
    assert captured.records[0][1]["items"] == [carried]


def test_cognitive_carry_selects_only_newest_terminal_and_reopens_on_hash_drift() -> (
    None
):
    source_ref = "spec:versioned"
    older = _cognitive_item(
        generation_id="generation-older",
        source_ref=source_ref,
        status="consolidated",
        content_hash="a" * 64,
        recorded_at="2026-08-14T08:00:00+00:00",
        event_ref="evt_older",
    )
    newer = _cognitive_item(
        generation_id="generation-newer",
        source_ref=source_ref,
        status="failed",
        content_hash="b" * 64,
        recorded_at="2026-08-14T09:00:00+00:00",
        event_ref="evt_newer",
    )
    snapshot = recovery.CognitiveLedgerBaseline(
        records=(
            (
                "generation-older",
                {
                    "board_id": BOARD_ID,
                    "kg_generation_id": "generation-older",
                    "recorded_at": "2026-08-14T08:00:00+00:00",
                    "items": [older],
                },
            ),
            (
                "generation-newer",
                {
                    "board_id": BOARD_ID,
                    "kg_generation_id": "generation-newer",
                    "recorded_at": "2026-08-14T09:00:00+00:00",
                    "items": [newer],
                },
            ),
        )
    )

    matching, unrelated = recovery._select_cognitive_carry_forward_rows(
        snapshot,
        target_generation_id="generation-target",
        expected_by_source={
            source_ref: {
                "source_ref": source_ref,
                "artifact_type": "spec",
                "content_hash": "b" * 64,
            }
        },
    )
    assert matching == {source_ref: newer}
    assert unrelated == {}

    reopened, _unrelated = recovery._select_cognitive_carry_forward_rows(
        snapshot,
        target_generation_id="generation-target",
        expected_by_source={
            source_ref: {
                "source_ref": source_ref,
                "artifact_type": "spec",
                # The older matching row must not be used after the newer
                # terminal row established the authoritative prior state.
                "content_hash": "a" * 64,
            }
        },
    )
    assert reopened == {}


@pytest.mark.parametrize(
    ("prior_hash", "expected_hash"),
    ((None, "a" * 64), ("a" * 64, None), (None, None)),
)
def test_cognitive_carry_reopens_when_either_hash_is_absent(
    prior_hash: str | None,
    expected_hash: str | None,
) -> None:
    source_ref = "spec:missing-hash"
    prior = _cognitive_item(
        generation_id="generation-prior",
        source_ref=source_ref,
        status="skipped",
        content_hash=prior_hash,
        recorded_at="2026-08-14T09:00:00+00:00",
        event_ref="evt_prior",
    )
    snapshot = recovery.CognitiveLedgerBaseline(
        records=(
            (
                "generation-prior",
                {
                    "board_id": BOARD_ID,
                    "kg_generation_id": "generation-prior",
                    "recorded_at": "2026-08-14T09:00:00+00:00",
                    "items": [prior],
                },
            ),
        )
    )

    carry, _unrelated = recovery._select_cognitive_carry_forward_rows(
        snapshot,
        target_generation_id="generation-target",
        expected_by_source={
            source_ref: {
                "source_ref": source_ref,
                "artifact_type": "spec",
                "content_hash": expected_hash,
            }
        },
    )

    assert carry == {}


def test_same_generation_terminal_disables_all_cross_generation_lookup() -> None:
    target_generation = "generation-target"
    same_source = "spec:same"
    prior_source = "spec:prior"
    unrelated_source = "spec:unrelated"
    # This is a chained carry: the target container contains a terminal row
    # whose own immutable identity still belongs to an older generation.
    same_terminal = _cognitive_item(
        generation_id="generation-original",
        source_ref=same_source,
        status="consolidated",
        content_hash="c" * 64,
        recorded_at="2026-08-13T09:00:00+00:00",
        event_ref="evt_original",
    )
    unrelated = _cognitive_item(
        generation_id=target_generation,
        source_ref=unrelated_source,
        status="in_progress",
        content_hash="u" * 64,
        recorded_at="2026-08-14T10:00:00+00:00",
        event_ref="evt_target",
    )
    prior_terminal = _cognitive_item(
        generation_id="generation-prior",
        source_ref=prior_source,
        status="skipped",
        content_hash="p" * 64,
        recorded_at="2026-08-14T09:00:00+00:00",
        event_ref="evt_prior",
    )
    snapshot = recovery.CognitiveLedgerBaseline(
        records=(
            (
                target_generation,
                {
                    "board_id": BOARD_ID,
                    "kg_generation_id": target_generation,
                    "recorded_at": "2026-08-14T10:00:00+00:00",
                    "items": [same_terminal, unrelated],
                },
            ),
            (
                "generation-prior",
                {
                    "board_id": BOARD_ID,
                    "kg_generation_id": "generation-prior",
                    "recorded_at": "2026-08-14T09:00:00+00:00",
                    "items": [prior_terminal],
                },
            ),
        )
    )
    expected = {
        same_source: {"content_hash": "c" * 64},
        prior_source: {"content_hash": "p" * 64},
    }

    carry, unrelated_rows = recovery._select_cognitive_carry_forward_rows(
        snapshot,
        target_generation_id=target_generation,
        expected_by_source=expected,
    )

    assert carry == {same_source: same_terminal}
    assert prior_source not in carry
    assert unrelated_rows == {unrelated_source: unrelated}


def test_cognitive_artifact_gate_accepts_live_like_cross_generation_carry(
    tmp_path: Path,
) -> None:
    prior_generation = "generation-prior"
    prior_recorded_at = "2026-08-14T09:00:00+00:00"
    prior_event = "evt_prior"
    expected_rows: list[dict[str, object]] = []
    carried_items: list[dict[str, object]] = []
    for index in range(18):
        source_ref = f"spec:carried-{index:02d}"
        content_hash = f"{index + 1:064x}"
        expected_rows.append(
            {
                "id": f"carried-{index:02d}",
                "source_ref": source_ref,
                "artifact_type": "spec",
                "content_hash": content_hash,
            }
        )
        carried_items.append(
            _cognitive_item(
                generation_id=prior_generation,
                source_ref=source_ref,
                status="consolidated",
                content_hash=content_hash,
                recorded_at=prior_recorded_at,
                event_ref=prior_event,
            )
        )
    prior_record: dict[str, object] = {
        "board_id": BOARD_ID,
        "kg_generation_id": prior_generation,
        "event_ref": prior_event,
        "pending_count": 0,
        "pending_refs": [],
        "status": "pending_marked",
        "recorded_at": prior_recorded_at,
        "items": carried_items,
    }
    rebuild_root, _baseline, kwargs = _terminal_artifact_fixture(
        tmp_path,
        resume_mode=True,
        baseline_cognitive_records=(prior_record,),
    )
    run_id = str(kwargs["run_id"])
    generation_id = str(kwargs["generation_id"])
    event_id = (
        "evt_"
        + __import__("hashlib")
        .sha256(f"{BOARD_ID}\x1f{run_id}".encode())
        .hexdigest()[:32]
    )
    current_recorded_at = "2026-08-15T10:00:00+00:00"
    pending_items: list[dict[str, object]] = []
    for index in range(11):
        source_ref = f"spec:pending-{index:02d}"
        content_hash = f"{index + 101:064x}"
        expected_rows.append(
            {
                "id": f"pending-{index:02d}",
                "source_ref": source_ref,
                "artifact_type": "spec",
                "content_hash": content_hash,
            }
        )
        pending_items.append(
            _cognitive_item(
                generation_id=generation_id,
                source_ref=source_ref,
                status="pending",
                content_hash=content_hash,
                recorded_at=current_recorded_at,
                event_ref=event_id,
            )
        )
    pending_refs = sorted(str(item["source_ref"]) for item in pending_items)
    cognitive_relative = f"audit/cognitive_pending/{BOARD_ID}/{generation_id}.json"
    _write_artifact(
        rebuild_root,
        cognitive_relative,
        {
            "board_id": BOARD_ID,
            "kg_generation_id": generation_id,
            "event_ref": event_id,
            "pending_count": 11,
            "pending_refs": pending_refs,
            "status": "pending_marked",
            "recorded_at": current_recorded_at,
            "items": [*carried_items, *pending_items],
        },
    )
    kwargs["expected_cognitive_source_rows"] = tuple(expected_rows)

    recovery._assert_rebuild_artifacts_governed(**kwargs)

    payload = dict(
        recovery._load_json_file(
            rebuild_root / cognitive_relative,
            "test_live_like_cognitive_payload",
        )
    )
    tampered = [dict(item) for item in payload["items"]]
    tampered[0]["kg_generation_id"] = generation_id
    payload["items"] = tampered
    _write_artifact(rebuild_root, cognitive_relative, payload)
    with pytest.raises(recovery.RecoveryRefused, match="item_binding_invalid"):
        recovery._assert_rebuild_artifacts_governed(**kwargs)


@pytest.mark.parametrize("unexpected_status", ("in_progress", "consolidated", "failed"))
def test_cognitive_artifact_gate_rejects_unproven_current_non_pending_status(
    tmp_path: Path,
    unexpected_status: str,
) -> None:
    rebuild_root, _baseline, kwargs = _terminal_artifact_fixture(
        tmp_path,
        resume_mode=True,
    )
    source_ref = "spec:unproven-status"
    source_row = {
        "id": "unproven-status",
        "source_ref": source_ref,
        "artifact_type": "spec",
        "content_hash": "e" * 64,
    }
    run_id = str(kwargs["run_id"])
    generation_id = str(kwargs["generation_id"])
    event_id = (
        "evt_"
        + __import__("hashlib")
        .sha256(f"{BOARD_ID}\x1f{run_id}".encode())
        .hexdigest()[:32]
    )
    recorded_at = "2026-08-15T10:00:00+00:00"
    active = unexpected_status in {"pending", "in_progress", "failed"}
    _write_artifact(
        rebuild_root,
        f"audit/cognitive_pending/{BOARD_ID}/{generation_id}.json",
        {
            "board_id": BOARD_ID,
            "kg_generation_id": generation_id,
            "event_ref": event_id,
            "pending_count": 1 if active else 0,
            "pending_refs": [source_ref] if active else [],
            "status": "pending_marked",
            "recorded_at": recorded_at,
            "items": [
                _cognitive_item(
                    generation_id=generation_id,
                    source_ref=source_ref,
                    status=unexpected_status,
                    content_hash=str(source_row["content_hash"]),
                    recorded_at=recorded_at,
                    event_ref=event_id,
                )
            ],
        },
    )
    kwargs["expected_cognitive_source_rows"] = (source_row,)

    with pytest.raises(recovery.RecoveryRefused, match="item_binding_invalid"):
        recovery._assert_rebuild_artifacts_governed(**kwargs)


@pytest.mark.parametrize(
    "extra_relative",
    (
        Path("audit") / "confirmation_receipts" / BOARD_ID / "evil-same-board.json",
        Path("audit")
        / "confirmation_receipts"
        / "00000000-0000-0000-0000-000000000001"
        / "run_0123456789abcdef01234567.json",
        Path("audit") / "events" / BOARD_ID / "evt_evil.json",
        Path("audit")
        / "events"
        / "00000000-0000-0000-0000-000000000001"
        / "evt_evil.json",
        Path("audit") / "cognitive_pending" / BOARD_ID / "other-generation.json",
        Path("audit")
        / "cognitive_pending"
        / "00000000-0000-0000-0000-000000000001"
        / "generation_terminal.json",
        Path("audit") / "confirmation" / BOARD_ID / f"audit_{'e' * 32}.json",
        Path("audit")
        / "confirmation"
        / "00000000-0000-0000-0000-000000000001"
        / f"audit_{'e' * 32}.json",
        Path("audit") / "unknown_nested" / BOARD_ID / "identity-linked.json",
    ),
)
def test_resume_artifact_gate_rejects_every_extra_nested_audit_family(
    tmp_path: Path,
    extra_relative: Path,
) -> None:
    rebuild_root, _baseline, kwargs = _terminal_artifact_fixture(
        tmp_path,
        resume_mode=True,
    )
    _write_artifact(
        rebuild_root,
        extra_relative,
        {
            "run_id": kwargs["run_id"],
            "manifest_ref": kwargs["manifest_ref"],
            "confirmation_ref": kwargs["confirmation_ref"],
            "board_id": BOARD_ID,
        },
    )

    with pytest.raises(recovery.RecoveryRefused):
        recovery._assert_rebuild_artifacts_governed(**kwargs)


@pytest.mark.parametrize(
    "terminal_overlay",
    (
        {
            "version": 1,
            "state": "stable",
            "revision": 9,
            "nonce": "terminal-overlay-nonce-0002",
        },
        {
            "version": 1,
            "state": "mutating",
            "revision": 8,
            "nonce": "terminal-overlay-nonce-0002",
        },
        {
            "version": 1,
            "state": "stable",
            "revision": 8,
            "nonce": "baseline-overlay-nonce-0001",
        },
    ),
)
def test_cognitive_overlay_gate_rejects_jump_pending_and_same_nonce(
    tmp_path: Path,
    terminal_overlay: dict[str, object],
) -> None:
    rebuild_root = tmp_path / "rebuild"
    baseline = {
        "version": 1,
        "state": "stable",
        "revision": 7,
        "nonce": "baseline-overlay-nonce-0001",
    }
    _write_artifact(
        rebuild_root,
        recovery._COGNITIVE_OVERLAY_REVISION_RELATIVE,
        terminal_overlay,
    )

    with pytest.raises(recovery.RecoveryRefused, match="overlay_revision"):
        recovery._assert_cognitive_overlay_revision_transition(
            rebuild_root=rebuild_root,
            baseline_payload=baseline,
            expect_advance=True,
        )


def test_frozen_terminal_requires_overlay_revision_to_remain_identical(
    tmp_path: Path,
) -> None:
    rebuild_root = tmp_path / "rebuild"
    baseline = {
        "version": 1,
        "state": "stable",
        "revision": 7,
        "nonce": "baseline-overlay-nonce-0001",
    }
    _write_artifact(
        rebuild_root,
        recovery._COGNITIVE_OVERLAY_REVISION_RELATIVE,
        baseline,
    )

    recovery._assert_cognitive_overlay_revision_transition(
        rebuild_root=rebuild_root,
        baseline_payload=baseline,
        expect_advance=False,
    )


def test_live_receipt_is_consumed_then_revalidated_under_lock_before_db_probe() -> None:
    source = inspect.getsource(recovery._execute)
    consume_offset = source.index("_register_live_consumption(")
    lock_offset = source.index("with acquire_serve_lock(settings) as serve_lock:")
    locked_revalidation_offset = source.index("bound_receipt_path=live_receipt_path")
    database_probe_offset = source.index("_assert_database_preflight(")

    assert consume_offset < lock_offset < locked_revalidation_offset
    assert locked_revalidation_offset < database_probe_offset


def test_post_teardown_board_snapshot_is_bound_before_attestation_and_completion() -> (
    None
):
    source = inspect.getsource(recovery._execute)
    lock_offset = source.index("with acquire_serve_lock(settings) as serve_lock:")
    lanes_offset = source.index("terminal = _run_bounded_recovery_lanes(")
    snapshot_offset = source.index("_capture_post_teardown_board_storage(")
    attach_offset = source.index('terminal["board_storage_post_teardown"]')
    heartbeat_stop_offset = source.index("heartbeat.stop()")
    attestation_offset = source.index("attestation = _build_rehearsal_attestation(")
    completion_offset = source.index('_emit("offline_recovery_completed"')

    assert (
        lock_offset
        < lanes_offset
        < snapshot_offset
        < attach_offset
        < heartbeat_stop_offset
        < attestation_offset
        < completion_offset
    )


def test_terminal_evidence_requires_exact_post_teardown_board_snapshot(
    tmp_path: Path,
) -> None:
    source_home = tmp_path / "live"
    source_home.mkdir()
    terminal = _terminal(source_home)

    validated = recovery._validate_rehearsal_terminal_evidence(
        terminal,
        source_home=source_home,
        source_storage=SOURCE_STORAGE,
    )
    assert validated["board_storage_post_teardown"] == {"graph.lbug": "a" * 64}

    with_sidecar = dict(terminal)
    with_sidecar["board_storage_post_teardown"] = {
        "graph.lbug": "a" * 64,
        "graph.lbug.wal": "b" * 64,
    }
    with pytest.raises(
        recovery.RecoveryRefused,
        match="rehearsal_terminal_board_storage_invalid",
    ):
        recovery._validate_rehearsal_terminal_evidence(
            with_sidecar,
            source_home=source_home,
            source_storage=SOURCE_STORAGE,
        )

    digest_mismatch = dict(terminal)
    digest_mismatch["board_storage_post_teardown_sha256"] = "f" * 64
    with pytest.raises(
        recovery.RecoveryRefused,
        match="rehearsal_terminal_board_storage_digest_mismatch",
    ):
        recovery._validate_rehearsal_terminal_evidence(
            digest_mismatch,
            source_home=source_home,
            source_storage=SOURCE_STORAGE,
        )


def test_attestation_is_path_bound_and_consumed_before_crash_cut(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_attestation_runtime(monkeypatch)
    source_home = tmp_path / "live"
    source_home.mkdir()
    receipt_path = tmp_path / "rehearsal.json"
    payload = _build_attestation(
        source_home=source_home,
        receipt_path=receipt_path,
    )
    recovery._write_new_json_file(receipt_path, payload, code="test_receipt")
    original_bytes = receipt_path.read_bytes()

    loaded, file_hash = recovery._validate_rehearsal_attestation(
        receipt_path=receipt_path,
        data_home=source_home,
        board_id=BOARD_ID,
        install_fingerprint=INSTALL_HASH,
        execution_contract=EXECUTION_CONTRACT,
    )
    consumed_path = recovery._register_live_consumption(
        receipt_path=receipt_path,
        receipt=loaded,
        receipt_file_hash=file_hash,
        data_home=source_home,
    )

    # This is the simulated crash cut: no live data-home function is called.
    # The durable consumed name already exists and the authorized name is gone.
    assert not receipt_path.exists()
    assert consumed_path.read_bytes() == original_bytes
    locked, locked_hash = recovery._validate_rehearsal_attestation(
        receipt_path=consumed_path,
        bound_receipt_path=receipt_path,
        data_home=source_home,
        board_id=BOARD_ID,
        install_fingerprint=INSTALL_HASH,
        execution_contract=EXECUTION_CONTRACT,
    )
    assert locked == loaded
    assert locked_hash == file_hash

    # Even if the original bytes are copied back after the crash, the durable
    # consumed target makes a second exclusive claim fail closed.
    shutil.copyfile(consumed_path, receipt_path)
    with pytest.raises(recovery.RecoveryRefused, match="already_exists"):
        recovery._register_live_consumption(
            receipt_path=receipt_path,
            receipt=loaded,
            receipt_file_hash=file_hash,
            data_home=source_home,
        )


def test_attestation_rejects_copy_tamper_expiry_and_missing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_attestation_runtime(monkeypatch)
    source_home = tmp_path / "live"
    source_home.mkdir()
    receipt_path = tmp_path / "rehearsal.json"
    payload = _build_attestation(
        source_home=source_home,
        receipt_path=receipt_path,
    )
    recovery._write_new_json_file(receipt_path, payload, code="test_receipt")

    copied = tmp_path / "copied.json"
    shutil.copyfile(receipt_path, copied)
    with pytest.raises(recovery.RecoveryRefused, match="path_mismatch"):
        recovery._validate_rehearsal_attestation(
            receipt_path=copied,
            data_home=source_home,
            board_id=BOARD_ID,
            install_fingerprint=INSTALL_HASH,
            execution_contract=EXECUTION_CONTRACT,
        )

    tampered = dict(payload)
    tampered["board_id"] = "00000000-0000-0000-0000-000000000000"
    receipt_path.write_bytes(recovery._canonical_json_bytes(tampered) + b"\n")
    with pytest.raises(recovery.RecoveryRefused, match="integrity_mismatch"):
        recovery._validate_rehearsal_attestation(
            receipt_path=receipt_path,
            data_home=source_home,
            board_id=BOARD_ID,
            install_fingerprint=INSTALL_HASH,
            execution_contract=EXECUTION_CONTRACT,
        )

    expired_path = tmp_path / "expired.json"
    expired = _build_attestation(
        source_home=source_home,
        receipt_path=expired_path,
        issued_at=datetime.now(timezone.utc)
        - timedelta(seconds=recovery.REHEARSAL_RECEIPT_TTL_SECONDS + 1),
    )
    recovery._write_new_json_file(expired_path, expired, code="expired_receipt")
    with pytest.raises(recovery.RecoveryRefused, match="expired"):
        recovery._validate_rehearsal_attestation(
            receipt_path=expired_path,
            data_home=source_home,
            board_id=BOARD_ID,
            install_fingerprint=INSTALL_HASH,
            execution_contract=EXECUTION_CONTRACT,
        )

    with pytest.raises(recovery.RecoveryRefused, match="unreadable"):
        recovery._validate_rehearsal_attestation(
            receipt_path=tmp_path / "missing.json",
            data_home=source_home,
            board_id=BOARD_ID,
            install_fingerprint=INSTALL_HASH,
            execution_contract=EXECUTION_CONTRACT,
        )


def test_entrypoint_hash_binds_metadata_and_launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata_path = Path("okto_pulse-0.3.2.dist-info/entry_points.txt")
    launcher_path = Path("bin/okto-pulse-kg-recovery-only.exe")
    (tmp_path / metadata_path).parent.mkdir(parents=True)
    (tmp_path / launcher_path).parent.mkdir(parents=True)
    (tmp_path / metadata_path).write_text(
        "[console_scripts]\n"
        "okto-pulse-kg-recovery-only = "
        "okto_pulse.community.kg_recovery_only:main\n",
        encoding="utf-8",
    )
    (tmp_path / launcher_path).write_bytes(b"launcher-v1")
    fake_distribution = SimpleNamespace(
        entry_points=(
            SimpleNamespace(
                group="console_scripts",
                name="okto-pulse-kg-recovery-only",
                value="okto_pulse.community.kg_recovery_only:main",
            ),
        ),
        files=(metadata_path, launcher_path),
        locate_file=lambda relative: tmp_path / relative,
    )
    monkeypatch.setattr(
        recovery.metadata,
        "distribution",
        lambda _name: fake_distribution,
    )

    first = recovery._hash_recovery_entrypoint_metadata()
    (tmp_path / launcher_path).write_bytes(b"launcher-v2")
    second = recovery._hash_recovery_entrypoint_metadata()

    assert first != second
    assert len(first) == len(second) == 64


def test_pyproject_publishes_recovery_console_script() -> None:
    project_root = Path(__file__).resolve().parents[1]
    config = tomllib.loads(
        (project_root / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert config["project"]["scripts"]["okto-pulse-kg-recovery-only"] == (
        "okto_pulse.community.kg_recovery_only:main"
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher ancestry regression")
def test_installed_wheel_launcher_allows_self_and_denies_second_launcher(
    tmp_path: Path,
) -> None:
    """Exercise the real distlib launcher without reaching a storage write."""

    import ctypes

    project_root = Path(__file__).resolve().parents[1]
    dist_dir = tmp_path / "dist"
    venv_dir = tmp_path / "venv"
    data_home = tmp_path / "copy"
    dist_dir.mkdir()
    data_home.mkdir()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(dist_dir),
            ".",
        ],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    subprocess.run(
        [sys.executable, "-m", "venv", "--system-site-packages", str(venv_dir)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    venv_python = venv_dir / "Scripts" / "python.exe"
    launcher = venv_dir / "Scripts" / "okto-pulse-kg-recovery-only.exe"
    wheel = next(dist_dir.glob("*.whl"))
    subprocess.run(
        [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--force-reinstall",
            "--disable-pip-version-check",
            str(wheel),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert launcher.is_file()
    common = [
        str(launcher),
        "--data-home",
        str(data_home),
        "--board-id",
        BOARD_ID,
    ]
    inspected = subprocess.run(
        [*common, "--inspect-install"],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    fingerprint = str(json.loads(inspected.stdout)["install_fingerprint"])
    receipt_path = tmp_path / "receipt.json"
    safe_rehearsal = [
        *common,
        "--rehearsal-copy-of",
        str(data_home),
        "--rehearsal-receipt-out",
        str(receipt_path),
        "--expected-install-fingerprint",
        fingerprint,
        "--offline-port",
        "65529",
    ]

    self_only = subprocess.run(
        safe_rehearsal,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert self_only.returncode == 2
    assert "rehearsal_source_equals_target" in self_only.stdout
    assert "offline_pulse_process_detected" not in self_only.stdout
    assert not receipt_path.exists()

    blocker = subprocess.Popen(
        [*common, "--inspect-install"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=0x08000000 | 0x00000004,  # NO_WINDOW | CREATE_SUSPENDED
    )
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    ntdll.NtResumeProcess.argtypes = [ctypes.c_void_p]
    ntdll.NtResumeProcess.restype = ctypes.c_long
    process_handle = kernel32.OpenProcess(0x0800 | 0x1000, False, blocker.pid)
    assert process_handle
    suspended = True
    try:
        denied = subprocess.run(
            safe_rehearsal,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert denied.returncode == 2
        assert "offline_pulse_process_detected" in denied.stdout
        assert str(blocker.pid) in denied.stdout
        assert not receipt_path.exists()
    finally:
        if suspended:
            ntdll.NtResumeProcess(process_handle)
        kernel32.CloseHandle(process_handle)
        try:
            blocker.wait(timeout=30)
        except subprocess.TimeoutExpired:
            blocker.kill()
            blocker.wait(timeout=10)
