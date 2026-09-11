"""Release contracts for removal of the retired embedded graph backend."""

from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from pydantic import ValidationError

from okto_pulse.community.config import CommunitySettings
from okto_pulse.community.adapters import graph_operation_guards as guards
from okto_pulse.community.adapters.graph_route_resolver import (
    CommunityGraphRouteResolver,
)
from okto_pulse.community.adapters.graph_backend_binding import (
    CommunityGraphBackendBindingStore,
)
from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphUnavailable,
    GraphLockContention,
)

ROOT = Path(__file__).resolve().parents[1]


def test_production_has_no_native_driver_import_or_dependency():
    import tomllib

    dependencies = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"][
        "dependencies"
    ]
    assert not any(d.split("=")[0].lower() in {"ladybug", "kuzu"} for d in dependencies)
    for path in (ROOT / "src").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            modules = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            assert not any(m.split(".")[0] in {"ladybug", "kuzu"} for m in modules), (
                path
            )


def test_settings_only_publish_supported_backend_and_constructor_knobs(tmp_path):
    settings = CommunitySettings(_env_file=None, data_dir=str(tmp_path))
    assert settings.kg_graph_backend == settings.kg_global_graph_backend == "grafx"
    assert not any("ladybug" in k or "kuzu" in k for k in type(settings).model_fields)
    with pytest.raises(ValidationError):
        CommunitySettings(
            _env_file=None, data_dir=str(tmp_path), kg_graph_backend="ladybug"
        )
    from okto_pulse.community.api.settings import (
        RuntimeSettingsResponse,
        RuntimeSettingsPayload,
    )
    from okto_pulse.community.adapters.sqlalchemy_runtime_settings_service import (
        GRAPH_DB_KEYS,
    )

    retired = {
        "kg_connection_pool_size",
        "kg_wal_salvage_enabled",
        "kg_wal_only_recovery_enabled",
    }
    for keys in (
        RuntimeSettingsResponse.model_fields,
        RuntimeSettingsPayload.model_fields,
        GRAPH_DB_KEYS,
    ):
        assert not any("ladybug" in k or "kuzu" in k or k in retired for k in keys)
    with pytest.raises(ValidationError):
        RuntimeSettingsPayload(kg_kuzu_buffer_pool_mb=256)


def test_budget_is_a_valid_rest_projection_without_invented_database_limit(tmp_path):
    from okto_pulse.community.adapters.graph_runtime_budget import (
        build_native_runtime_budget_snapshot,
    )
    from okto_pulse.community.api.kg_health import NativeRuntimeBudget
    from dataclasses import fields

    settings = CommunitySettings(
        _env_file=None,
        data_dir=str(tmp_path),
        kg_grafx_buffer_pool_mb=64,
        kg_grafx_read_participants=3,
    )
    snapshot = build_native_runtime_budget_snapshot(settings)
    model = NativeRuntimeBudget.model_validate(
        {field.name: getattr(snapshot, field.name) for field in fields(snapshot)}
    )
    assert model.effective.max_db_size_gb is None
    assert model.effective.board_buffer_pool_mb == 64
    assert model.process_envelope.buffer_pool_per_board_mb == 256
    assert model.process_envelope.total_process_bound_available is False


@pytest.mark.parametrize("deny", [False, True])
def test_explicit_privacy_erases_only_board_graph_artifacts_under_fence(tmp_path, deny):
    from okto_pulse.community.adapters.grafx_board_storage import (
        grafx_board_privacy_scope,
        erase_grafx_board_privacy_storage,
    )

    board_root = tmp_path / "boards" / "privacy-board"
    board_root.mkdir(parents=True)
    scope = grafx_board_privacy_scope("privacy-board", board_root)
    graph = board_root / "graph.lbug"
    wal = board_root / "graph.lbug.wal"
    binding = board_root / "graph_backend_binding.json"
    preserved = board_root / "notes.txt"
    for p in (graph, wal, binding, preserved):
        p.write_bytes(b"opaque bytes")
    checks = []

    def fence():
        checks.append(True)
        assert binding.exists(), "binding must be erased last"
        if deny:
            raise PermissionError("fence lost")

    if deny:
        with pytest.raises(PermissionError, match="fence lost"):
            erase_grafx_board_privacy_storage(scope, before_mutation=fence)
        assert all(p.read_bytes() == b"opaque bytes" for p in (graph, wal, binding))
    else:
        assert erase_grafx_board_privacy_storage(scope, before_mutation=fence) == 3
        assert not any(p.exists() for p in (graph, wal, binding))
    assert checks
    assert preserved.read_bytes() == b"opaque bytes"


def test_offline_executor_rejects_legacy_files_without_mutating_them(tmp_path):
    from okto_pulse.community import kg_recovery_only as recovery

    board = tmp_path / "boards" / "retired"
    board.mkdir(parents=True)
    graph = board / "graph.lbug"
    graph.write_bytes(b"preserve")
    with pytest.raises(recovery.RecoveryRefused, match="retired_files_preserved"):
        recovery._require_authenticated_recoverable_backend(tmp_path, "retired")
    assert graph.read_bytes() == b"preserve"
    assert list(board.iterdir()) == [graph]


@pytest.mark.parametrize("scope", ["board", "global"])
def test_legacy_files_refused_without_binding_publication_or_mutation(tmp_path, scope):
    store = CommunityGraphBackendBindingStore(tmp_path)
    resolver = CommunityGraphRouteResolver(
        store, board_backend="grafx", global_backend="grafx", grafx_page_size=8192
    )
    path = (
        store.board_ladybug_path("old-board")
        if scope == "board"
        else store.global_ladybug_path()
    )
    path.parent.mkdir(parents=True)
    path.write_bytes(b"legacy database bytes must survive")
    before = {
        str(p.relative_to(tmp_path)): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    with pytest.raises(GraphUnavailable, match="route is unavailable"):
        if scope == "board":
            resolver.initialize_board_route(
                "old-board", create_physical=lambda _: pytest.fail("must not create")
            )
        else:
            resolver.initialize_global_route(
                create_physical=lambda _: pytest.fail("must not create")
            )
    assert path.read_bytes() == b"legacy database bytes must survive"
    assert not list(tmp_path.rglob("*binding*.json"))
    assert all(
        (tmp_path / relative).read_bytes() == content
        for relative, content in before.items()
    )


def test_readers_are_parallel_and_storage_mutation_fails_closed():
    board = "grafx-only-concurrency"
    entered, release = Event(), Event()

    def reader():
        with guards.board_graph_operation_window(board):
            entered.set()
            assert release.wait(3)

    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(reader)
        try:
            assert entered.wait(3)
            with guards.board_graph_operation_window(board):
                assert guards._get_close_guard(board).readers == 2
            with pytest.raises(GraphLockContention):
                with guards.board_storage_mutation_window(
                    board, phase="test", drain_timeout=0.02
                ):
                    pytest.fail("must not enter while a reader is active")
            with guards.board_storage_mutation_window(
                "independent-board", phase="test"
            ):
                pass
        finally:
            release.set()
        future.result(timeout=3)
    with guards.board_storage_mutation_window(board, phase="test"):
        assert guards._get_close_guard(board).readers == 0


def test_app_composition_cannot_import_retired_native_driver(tmp_path):
    script = """
import importlib.abc, sys
class BlockNative(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'ladybug', 'kuzu'}:
            raise AssertionError('retired native import: '+fullname)
sys.meta_path.insert(0, BlockNative())
from okto_pulse.community.config import CommunitySettings
from okto_pulse.community.adapters.routed_graph_composition import build_community_routed_graph_composition
from okto_pulse.community.main import create_community_app
settings = CommunitySettings(_env_file=None)
bundle = build_community_routed_graph_composition(settings=settings)
assert len(bundle.registry_providers()) == 10
from okto_pulse.core.infra.config import configure_settings
configure_settings(lambda: settings)
app = create_community_app()
assert app is not None
print('grafx-only app composed')
"""
    env = dict(os.environ, DATA_DIR=str(tmp_path), KG_BASE_DIR=str(tmp_path / "kg"))
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "grafx-only app composed" in result.stdout
