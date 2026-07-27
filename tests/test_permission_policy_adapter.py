"""F08 Community conformance tests for the public permission policy port."""

from __future__ import annotations

import ast
from pathlib import Path

from okto_pulse.core.ports.permission_policy import (
    DefaultPermissionPolicy,
    PermissionContext,
    PermissionPolicyPort,
)
from okto_pulse.community.adapters.permission_policy import (
    CommunityPermissionPolicyAdapter,
)
from okto_pulse.community.adapters.relational_application import (
    CommunityRelationalApplicationAdapter,
)


COMMUNITY_ROOT = (
    Path(__file__).resolve().parents[1] / "src" / "okto_pulse" / "community"
)


def test_community_adapter_implements_public_contract_with_policy_parity() -> None:
    adapter = CommunityPermissionPolicyAdapter()
    canonical = DefaultPermissionPolicy()

    assert isinstance(adapter, PermissionPolicyPort)
    local = adapter.resolve(
        agent_flags={"board": {"read": True}},
        preset_flags=None,
        board_overrides={"board": {"read": False}},
    )
    expected = canonical.resolve(
        agent_flags={"board": {"read": True}},
        preset_flags=None,
        board_overrides={"board": {"read": False}},
    )
    assert local.flags == expected.flags
    assert adapter.evaluate(PermissionContext("board.read", local)) == canonical.evaluate(
        PermissionContext("board.read", expected)
    )


def test_relational_bundle_injects_one_permission_policy_instance() -> None:
    policy = CommunityPermissionPolicyAdapter()
    bundle = CommunityRelationalApplicationAdapter(policy)
    session = object()

    preset_gateway = bundle.permission_presets(session)  # type: ignore[arg-type]
    auth_gateway = bundle.agent_authentication(session)  # type: ignore[arg-type]

    assert preset_gateway._permission_policy is policy
    assert auth_gateway._permission_policy is policy


def test_community_production_code_has_no_private_permission_reach_in() -> None:
    violations: list[tuple[str, int]] = []
    for path in sorted(COMMUNITY_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            module = None
            if isinstance(node, ast.ImportFrom):
                module = node.module
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("okto_pulse.core.infra.permissions"):
                        violations.append((str(path.relative_to(COMMUNITY_ROOT)), node.lineno))
            if module and module.startswith("okto_pulse.core.infra.permissions"):
                violations.append((str(path.relative_to(COMMUNITY_ROOT)), node.lineno))
    assert violations == []
