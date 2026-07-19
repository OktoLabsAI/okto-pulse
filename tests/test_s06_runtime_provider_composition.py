"""S06 Community Local First provider ownership coverage."""

from __future__ import annotations

import ast
from pathlib import Path

from okto_pulse.core.kg.interfaces.registry import (
    get_kg_registry,
    reset_registry_for_tests,
)


def test_community_composition_owns_all_local_runtime_providers() -> None:
    from okto_pulse.community.adapters.composition import configure_community_kg_registry

    reset_registry_for_tests()
    try:
        configure_community_kg_registry(object())
        registry = get_kg_registry()
        for provider in (
            registry.cache_backend,
            registry.rate_limiter,
            registry.session_store,
            registry.config,
        ):
            assert type(provider).__module__.startswith("okto_pulse.community.adapters")
    finally:
        reset_registry_for_tests()


def test_community_composition_normalizes_legacy_core_settings_to_one_root(
    tmp_path,
    monkeypatch,
) -> None:
    import okto_pulse.core.infra.config as core_config
    from okto_pulse.community.adapters.composition import (
        configure_community_kg_registry,
    )
    from okto_pulse.core.infra.config import CoreSettings

    original = core_config.get_settings()
    kg_root = (tmp_path / "kg-root").resolve()
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KG_BASE_DIR", str(kg_root))
    core_config.configure_settings(CoreSettings())
    reset_registry_for_tests()
    try:
        configure_community_kg_registry(object())
        registry = get_kg_registry()
        assert Path(registry.config.kg_base_dir) == kg_root
        assert registry.rebuild_audit_artifact_store._base_dir == kg_root  # noqa: SLF001
    finally:
        reset_registry_for_tests()
        core_config.configure_settings(original)


def test_community_composition_never_imports_core_testing_fakes() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "src/okto_pulse/community/adapters/composition.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = {
        (node.module or "")
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert not any(module.startswith("okto_pulse.core.kg.providers.testing") for module in imported)
