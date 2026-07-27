"""F04 Community pre-application composition and ordering tests."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from okto_pulse.core.composition import RuntimeProviderMissing
from okto_pulse.community.adapters.runtime_composition import (
    CommunitySettingsSnapshotProvider,
    build_community_runtime_composition,
)


MAIN_PATH = Path(__file__).resolve().parents[1] / "src" / "okto_pulse" / "community" / "main.py"


def _providers(**overrides):
    providers = {
        "settings": object(),
        "auth_provider": object(),
        "storage_provider": object(),
        "event_bus": object(),
        "scheduler_control": object(),
        "uow_factory": object(),
        "worker_registry": object(),
        "content_ingestion_resolver": object(),
    }
    providers.update(overrides)
    return providers


def test_builder_returns_complete_immutable_provider_identity() -> None:
    providers = _providers()
    composition = build_community_runtime_composition(**providers)

    assert isinstance(
        composition.settings_provider,
        CommunitySettingsSnapshotProvider,
    )
    assert (
        composition.settings_provider.get_settings_snapshot()
        is providers["settings"]
    )
    assert composition.auth_provider is providers["auth_provider"]
    assert composition.uow_factory is providers["uow_factory"]
    assert composition.worker_registry is providers["worker_registry"]
    assert composition.content_ingestion_resolver is providers[
        "content_ingestion_resolver"
    ]
    assert composition.missing_required() == []


def test_builder_fails_before_app_and_names_missing_required_provider() -> None:
    with pytest.raises(RuntimeProviderMissing) as exc:
        build_community_runtime_composition(**_providers(event_bus=None))
    assert exc.value.provider_key == "event_bus"
    assert exc.value.missing == ["event_bus"]


def test_main_builds_and_registers_providers_before_create_app() -> None:
    tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"), filename=str(MAIN_PATH))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "create_community_app"
    )
    calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]

    def call_name(call: ast.Call) -> str:
        func = call.func
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
        return ""

    create_line = min(call.lineno for call in calls if call_name(call) == "create_app")
    build_line = min(
        call.lineno
        for call in calls
        if call_name(call) == "build_community_runtime_composition"
    )
    assert build_line < create_line

    late_registrations = [
        (call.lineno, call_name(call))
        for call in calls
        if call.lineno > create_line
        and (
            call_name(call).startswith("register_")
            or call_name(call).startswith("configure_")
        )
    ]
    assert late_registrations == []


def test_lifespan_uses_the_worker_registry_owned_by_the_app_composition() -> None:
    source = MAIN_PATH.read_text(encoding="utf-8")
    assert "app_instance.state.runtime_composition.worker_registry" in source
    tree = ast.parse(source, filename=str(MAIN_PATH))
    create_function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "create_community_app"
    )
    lifespan = next(
        node
        for node in create_function.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "combined_lifespan"
    )
    called_names = {
        node.func.id
        for node in ast.walk(lifespan)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "build_community_worker_registry" not in called_names


def test_lifespan_applies_persisted_settings_before_background_runtime() -> None:
    tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"), filename=str(MAIN_PATH))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "create_community_app"
    )
    lifespan = next(
        node
        for node in function.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "combined_lifespan"
    )

    def call_name(call: ast.Call) -> str:
        func = call.func
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
        return ""

    calls = [node for node in ast.walk(lifespan) if isinstance(node, ast.Call)]
    apply_line = min(
        call.lineno
        for call in calls
        if call_name(call) == "apply_persisted_settings_to_core_settings"
    )
    runtime_start_lines = [
        call.lineno
        for call in calls
        if call_name(call)
        in {
            "seed_community_defaults",
            "create_task",
            "_preload_embedding_model",
            "start_all",
        }
    ]

    assert runtime_start_lines
    assert apply_line < min(runtime_start_lines)
