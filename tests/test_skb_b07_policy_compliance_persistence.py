"""Retirement ratchet for deterministic policy/v1 compliance persistence.

The semantic SK-B3 persistence suite is authoritative.  These small checks
keep the removed evaluator from silently returning through a compatibility
import while proving the replacement domain remains importable.
"""

from __future__ import annotations

from importlib.util import find_spec

from okto_pulse.core.domain.guideline_semantic_assessment import (
    SemanticAssessmentState,
    SemanticMetricOutcome,
)


def test_deterministic_policy_evaluator_module_remains_absent() -> None:
    assert find_spec("okto_pulse.core.domain.guideline_policy_evaluator") is None


def test_semantic_assessment_contract_is_the_active_replacement() -> None:
    assert {item.value for item in SemanticAssessmentState} == {
        "passed",
        "metric_threshold_failed",
    }
    assert {item.value for item in SemanticMetricOutcome} == {"pass", "fail"}
