"""Failure boundaries for the production router, not a Core in-memory fake."""

import pytest
from okto_pulse.core.kg.global_discovery_writer import GlobalDiscoveryWriterFenceLost
from okto_pulse.community.adapters.routed_global_graph_composition import (
    _default_fence_revalidator,
)
from test_routed_global_discovery import _Resolver, _RuntimeHarness, _snapshot


@pytest.mark.parametrize("operation", ["bootstrap", "purge"])
def test_raw_global_mutations_require_durable_lease_before_physical_dispatch(
    tmp_path, operation
):
    harness = _RuntimeHarness(_Resolver(_snapshot(tmp_path, backend="grafx")))
    harness.runtime._revalidate_write_fence = _default_fence_revalidator
    with pytest.raises(GlobalDiscoveryWriterFenceLost):
        getattr(harness.runtime, operation)()
    assert harness.grafx_factory.entered == 0
    assert harness.grafx_factory.provider.calls == []
