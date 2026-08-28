"""Blocker 1: the runtime-checkable Core recovery Protocol must include the
unified ``recover_and_cutover`` the production worker invokes, so an old-only
provider is rejected by ``isinstance`` instead of passing and then failing with
a late ``AttributeError``.
"""

from __future__ import annotations

from okto_pulse.core.kg.interfaces.global_discovery_recovery import (
    GlobalDiscoveryRecovery,
)
from typing_extensions import get_protocol_members

from okto_pulse.community.adapters.global_discovery_recovery import (
    CommunityGlobalDiscoveryRecovery,
)


class _OldOnlyProvider:
    """A provider that predates the unified entry (no ``recover_and_cutover``)."""

    def inspect_live_artifact(self):
        raise NotImplementedError

    def rebuild_candidate_and_cutover(self, **_kwargs):
        raise NotImplementedError

    def current_snapshot_fingerprint(self):
        raise NotImplementedError


class _CompleteProvider(_OldOnlyProvider):
    def recover_and_cutover(self, **_kwargs):
        raise NotImplementedError


def test_recover_and_cutover_is_in_the_core_protocol():
    assert "recover_and_cutover" in get_protocol_members(GlobalDiscoveryRecovery)


def test_old_only_provider_is_rejected_by_isinstance():
    # Missing recover_and_cutover -> runtime protocol check fails (no late
    # AttributeError at call time).
    assert not isinstance(_OldOnlyProvider(), GlobalDiscoveryRecovery)


def test_complete_provider_is_accepted_by_isinstance():
    assert isinstance(_CompleteProvider(), GlobalDiscoveryRecovery)


def test_production_adapter_satisfies_the_protocol():
    # The real Community adapter must satisfy the updated protocol.
    assert hasattr(CommunityGlobalDiscoveryRecovery, "recover_and_cutover")
    assert hasattr(CommunityGlobalDiscoveryRecovery, "rebuild_candidate_and_cutover")
