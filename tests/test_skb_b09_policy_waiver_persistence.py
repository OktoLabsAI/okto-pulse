"""Current exception lifecycles remain distinct.

Semantic metric waivers and human skips now own exception governance; their
full persistence coverage lives in the SK-B3 semantic suites.
"""

from __future__ import annotations

from okto_pulse.core.domain.guideline_semantic_exceptions import (
    SemanticMetricWaiverEventType,
    SemanticMetricWaiverStatus,
    SemanticPolicySkipEventType,
    SemanticPolicySkipStatus,
)


def test_semantic_metric_waiver_lifecycle_is_closed() -> None:
    assert {item.value for item in SemanticMetricWaiverStatus} == {
        "requested",
        "approved",
        "rejected",
        "revoked",
        "expired",
    }
    assert {item.value for item in SemanticMetricWaiverEventType} == {
        "request",
        "approve",
        "reject",
        "revoke",
        "expire",
        "revalidate",
    }


def test_human_skip_lifecycle_is_distinct_from_metric_waivers() -> None:
    assert {item.value for item in SemanticPolicySkipStatus} == {
        "active",
        "revoked",
    }
    assert {item.value for item in SemanticPolicySkipEventType} == {
        "create",
        "revoke",
    }
