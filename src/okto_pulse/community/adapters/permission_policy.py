"""Local First implementation of the public permission policy contract."""

from __future__ import annotations

from okto_pulse.core.ports.permission_policy import (
    DefaultPermissionPolicy,
    PermissionContext,
    PermissionDecision,
    PermissionFlags,
    PermissionPolicyPort,
    PermissionSet,
    normalize_agent_permission_layer,
)


class CommunityPermissionPolicyAdapter:
    """Community adapter that delegates decisions to canonical Core policy.

    Community owns loading and composing Local First permission data.  Core
    remains the sole owner of merge, ceiling and state-transition semantics.
    """

    def __init__(self, policy: PermissionPolicyPort | None = None) -> None:
        self._policy = policy or DefaultPermissionPolicy()

    def resolve(
        self,
        agent_flags: PermissionFlags | None,
        preset_flags: PermissionFlags | None,
        board_overrides: PermissionFlags | None,
        *,
        owner_review_required: bool = False,
        review_reason: str | None = None,
    ) -> PermissionSet:
        return self._policy.resolve(
            agent_flags,
            preset_flags,
            board_overrides,
            owner_review_required=owner_review_required,
            review_reason=review_reason,
        )

    def evaluate(self, context: PermissionContext) -> PermissionDecision:
        return self._policy.evaluate(context)


def direct_permission_review(
    agent_flags: object,
    *,
    preset_id: str | None,
) -> tuple[bool, str | None]:
    """Classify a preset-less persisted granular document for safe upgrade.

    ``None`` is the trusted Full Control sentinel.  A recognized historical or
    current Full Control snapshot normalizes back to that sentinel.  Any other
    preset-less document lacks durable lineage/fingerprint provenance and must
    remain denied until the owner reviews it.
    """

    if preset_id is not None or agent_flags is None:
        return False, None
    if not isinstance(agent_flags, dict):
        return True, "invalid_agent_flags"
    try:
        normalized = normalize_agent_permission_layer(agent_flags)
    except (TypeError, ValueError):
        return True, "invalid_agent_flags"
    if normalized is None:
        return False, None
    return True, "unrecognized_direct_permissions"


__all__ = [
    "CommunityPermissionPolicyAdapter",
    "direct_permission_review",
]
