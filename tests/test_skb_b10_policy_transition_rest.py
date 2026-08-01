"""SK-B B10 REST projection and mutation-boundary coverage."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.core.domain.guideline_policy import (
    GuidelineEnforcement,
    PolicyCurrentness,
    PolicyEntityType,
)
from okto_pulse.core.domain.guideline_policy_transition import (
    PolicyTransitionDecision,
    PolicyTransitionDiagnosticCode,
    PolicyTransitionReasonCode,
    PolicyTransitionRejected,
    SemanticBindingComplianceDecision,
)
from okto_pulse.core.domain.guideline_semantic_currentness import (
    SemanticAssessmentCurrentnessReason,
)
from okto_pulse.core.inbound.policy_transition_error import (
    project_policy_transition_rejection,
)


API_ROOT = (
    Path(__file__).resolve().parents[1] / "src" / "okto_pulse" / "community" / "api"
)
REST_MUTATION_HANDLERS = (
    ("ideations.py", "move_ideation"),
    ("refinements.py", "move_refinement"),
    ("specs.py", "move_spec"),
    ("sprints.py", "move_sprint"),
    ("cards.py", "move_card"),
    ("cards.py", "submit_task_validation"),
    ("specs.py", "submit_spec_validation"),
    ("specs.py", "update_test_scenario_status"),
)


def _rejection() -> PolicyTransitionRejected:
    binding = SemanticBindingComplianceDecision(
        binding_id="binding-1",
        guideline_id="guideline-1",
        enforcement=GuidelineEnforcement.BLOCKING,
        applicable_metric_count=4,
        allowed=False,
        assessment_available=True,
        receipt_id="receipt-1",
        currentness=PolicyCurrentness.STALE,
        currentness_reasons=(
            SemanticAssessmentCurrentnessReason.POLICY_SET_CHANGED,
        ),
        inadmissibility_cause=None,
        failed_metric_count=0,
        waived_metric_count=0,
        blocking_metric_count=0,
        advisory_issue_count=0,
        skipped=False,
        diagnostic_codes=(
            PolicyTransitionDiagnosticCode.POLICY_COMPLIANCE_RECEIPT_STALE,
        ),
    )
    return PolicyTransitionRejected(
        PolicyTransitionDecision(
            entity_type=PolicyEntityType.CARD,
            subject_id="card-1",
            from_status="validation",
            to_status="done",
            transition_allowed=True,
            policy_compliance_required=True,
            allowed=False,
            reason_codes=(
                PolicyTransitionReasonCode.POLICY_COMPLIANCE_RECEIPT_STALE,
            ),
            diagnostic_codes=binding.diagnostic_codes,
            binding_decisions=(binding,),
            receipt_ids=("receipt-1",),
            applicable_metric_count=4,
            applicable_blocking_metric_count=4,
            failed_metric_count=0,
            blocking_metric_count=0,
            waived_metric_count=0,
            advisory_issue_count=0,
            skipped_binding_count=0,
            fence_digest="f" * 64,
        )
    )


def _caught_names(node: ast.expr | None) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Attribute):
        return {node.attr}
    if isinstance(node, ast.Tuple):
        names: set[str] = set()
        for element in node.elts:
            names.update(_caught_names(element))
        return names
    return set()


def _function(tree: ast.Module, name: str) -> ast.AsyncFunctionDef:
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"handler_not_found:{name}")


def test_rest_adapter_maps_policy_rejection_to_shared_409_envelope() -> None:
    error = _rejection()

    response = RESTAdapterContract.http_error(error)

    assert response.status_code == 409
    assert response.detail == project_policy_transition_rejection(error)


@pytest.mark.parametrize(("file_name", "handler_name"), REST_MUTATION_HANDLERS)
def test_rest_mutations_route_policy_rejection_through_shared_projection(
    file_name: str,
    handler_name: str,
) -> None:
    tree = ast.parse((API_ROOT / file_name).read_text(encoding="utf-8"))
    handler = _function(tree, handler_name)
    matching_tries = []
    for candidate in ast.walk(handler):
        if not isinstance(candidate, ast.Try):
            continue
        caught = [_caught_names(item.type) for item in candidate.handlers]
        if any("PolicyTransitionRejected" in names for names in caught):
            matching_tries.append((candidate, caught))

    assert len(matching_tries) == 1
    candidate, caught = matching_tries[0]
    policy_index = next(
        index
        for index, names in enumerate(caught)
        if "PolicyTransitionRejected" in names
    )
    value_indexes = [
        index for index, names in enumerate(caught) if "ValueError" in names
    ]
    assert not value_indexes or policy_index < value_indexes[0]
    policy_handler = candidate.handlers[policy_index]
    assert any(
        isinstance(node, ast.Attribute)
        and node.attr == "http_error"
        and isinstance(node.value, ast.Name)
        and node.value.id == "RESTAdapterContract"
        for node in ast.walk(policy_handler)
    )
