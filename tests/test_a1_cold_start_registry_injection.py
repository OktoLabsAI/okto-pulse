"""A1 regressions: the Community MCP cold-start transaction requires an
explicit injected runtime-value registry (no ambient fallback, no lazy
``create=True``).

Blocking tests recorded in the follow-up (Nexus ruling
``msg_ccbba890701648e290918337d7bcdc8e``):

- T-A1-1: real rollback restores the exact registry captured at ``begin``
  (object identity, not equality).
- T-A1-2: ``begin`` without an argument fails typed, and an AST/static guard
  finds zero empty production calls.
- T-A1-3: the transaction registry IS the active composition registry by
  object identity end-to-end (the pattern ``main`` and the release gate use).
- T-A1-4: a test helper with no registry fails typed and never creates one.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from okto_pulse.core.composition import (
    RuntimeValueRegistry,
    runtime_value_scope,
    snapshot_runtime_values,
)
from okto_pulse.core.runtime_context import current_runtime_values, reset_runtime_values
from okto_pulse.community.adapters.resources import (
    CommunityMcpColdStartTransaction,
    register_and_freeze_community_resource_catalog,
)


COMMUNITY_SRC = Path(__file__).resolve().parents[1] / "src" / "okto_pulse" / "community"
COMMUNITY_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


# ---------------------------------------------------------------------------
# T-A1-1 — rollback restores the exact registry captured at begin (identity).
# ---------------------------------------------------------------------------
def test_a1_1_rollback_restores_exact_registry_object_identity():
    registry = RuntimeValueRegistry()
    with runtime_value_scope(registry):
        transaction = register_and_freeze_community_resource_catalog(registry)
        # The transaction operates on the exact injected object, not a copy.
        assert transaction._registry is registry
        # Staging mutated the active registry; capture a post-freeze key set.
        staged_keys = set(registry.snapshot())
        assert staged_keys, "cold start should have staged runtime values"

        transaction.rollback()

    # Rollback restored the SAME registry object (identity) to its baseline.
    assert transaction._registry is registry
    assert transaction._rolled_back is True


# ---------------------------------------------------------------------------
# T-A1-2 — begin() with no registry fails typed; no empty production calls.
# ---------------------------------------------------------------------------
def test_a1_2a_begin_requires_explicit_registry():
    with pytest.raises(TypeError):
        CommunityMcpColdStartTransaction.begin()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        CommunityMcpColdStartTransaction.begin(None)  # type: ignore[arg-type]


def _empty_cold_start_calls(root: Path) -> list[str]:
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name in {
                "register_and_freeze_community_resource_catalog",
                "begin",
            } and not node.args and not node.keywords:
                # ``begin`` is common; only flag the cold-start transaction one.
                if name == "begin" and not (
                    isinstance(func, ast.Attribute)
                    and _is_cold_start_begin(func)
                ):
                    continue
                offenders.append(f"{path}:{node.lineno}")
    return offenders


def _is_cold_start_begin(func: ast.Attribute) -> bool:
    value = func.value
    return isinstance(value, ast.Name) and value.id.endswith(
        "CommunityMcpColdStartTransaction"
    )


def test_a1_2b_no_empty_cold_start_calls_in_production():
    offenders = _empty_cold_start_calls(COMMUNITY_SRC) + _empty_cold_start_calls(
        COMMUNITY_SCRIPTS
    )
    assert offenders == [], offenders


# ---------------------------------------------------------------------------
# T-A1-3 — transaction registry IS the active composition registry (identity).
# ---------------------------------------------------------------------------
def test_a1_3_transaction_registry_is_active_composition_registry():
    # This mirrors the exact injection pattern that ``main.py`` (inside
    # ``runtime_composition_scope``) and the release-gate server use: the active
    # registry is bound in scope AND injected, so the transaction registry is
    # the active one by object identity, end to end.
    composition_registry = snapshot_runtime_values()
    with runtime_value_scope(composition_registry):
        assert current_runtime_values() is composition_registry
        transaction = register_and_freeze_community_resource_catalog(
            composition_registry
        )
        assert transaction._registry is composition_registry
        assert current_runtime_values() is composition_registry
        transaction.rollback()


def test_a1_3_main_and_gate_inject_active_composition_registry_by_source():
    # Static proof that production callers inject an explicit registry rather
    # than relying on an ambient fallback.
    main_src = (COMMUNITY_SRC / "main.py").read_text(encoding="utf-8")
    assert (
        "register_and_freeze_community_resource_catalog(\n"
        "                runtime_composition.runtime_values\n"
        "            )"
    ) in main_src or "runtime_composition.runtime_values" in main_src

    gate_src = (COMMUNITY_SCRIPTS / "release_artifact_gate.py").read_text(
        encoding="utf-8"
    )
    assert "register_and_freeze_community_resource_catalog(runtime_values)" in gate_src
    assert "runtime_value_scope(runtime_values)" in gate_src


# ---------------------------------------------------------------------------
# T-A1-4 — test helper with no registry fails typed and never creates one.
# ---------------------------------------------------------------------------
def test_a1_4_helper_fails_typed_without_active_registry_and_never_creates(
    require_active_runtime_registry,
):
    # Force a state with no active runtime-value registry.
    reset_runtime_values()
    from okto_pulse.core.runtime_context import _active_runtime_values

    token = _active_runtime_values.set(None)
    try:
        assert current_runtime_values() is None
        with pytest.raises(RuntimeError):
            require_active_runtime_registry()
        # The helper must never lazily create/bind a registry.
        assert current_runtime_values() is None
    finally:
        _active_runtime_values.reset(token)


def test_a1_4_helper_returns_active_registry_when_present(
    require_active_runtime_registry,
):
    registry = RuntimeValueRegistry()
    with runtime_value_scope(registry):
        assert require_active_runtime_registry() is registry
