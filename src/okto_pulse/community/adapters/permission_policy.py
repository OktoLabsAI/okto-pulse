"""Local First implementation of the public permission policy contract."""

from __future__ import annotations

from okto_pulse.core.ports.permission_policy import (
    DefaultPermissionPolicy,
    PermissionContext,
    PermissionDecision,
    PermissionFlags,
    PermissionPolicyPort,
    PermissionSet,
    direct_permission_review,
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


__all__ = [
    "CommunityPermissionPolicyAdapter",
    "direct_permission_review",
]
