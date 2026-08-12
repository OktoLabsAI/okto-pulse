"""REST projections for lifecycle Draft-only semantic writes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest
from fastapi import HTTPException, Response

from okto_pulse.community.api import (
    architecture,
    ideations,
    refinements,
    resource_gate,
    specs,
)
from okto_pulse.core.domain.human_validation_cycle import (
    LifecycleTransitionConflictError,
    SubjectEditRequiresDraftError,
)
from okto_pulse.core.inbound.human_validation_cycle_error import (
    project_subject_edit_requires_draft_error,
)


class _NoWriteUow:
    def __init__(self) -> None:
        self.commit_calls = 0
        self.rollback_calls = 0

    async def commit(self) -> None:
        self.commit_calls += 1

    async def rollback(self) -> None:
        self.rollback_calls += 1


RouteInvocation = Callable[[_NoWriteUow], Awaitable[object]]


_CASES: tuple[tuple[type, RouteInvocation, str, str], ...] = (
    (
        ideations.UpdateIdeationUseCase,
        lambda uow: ideations.update_ideation(
            "ideation-1",
            ideations.IdeationUpdate(title="Changed"),
            user_id="user-1",
            uow=uow,
        ),
        "ideation",
        "ideation-1",
    ),
    (
        ideations.CreateIdeationKnowledgeUseCase,
        lambda uow: ideations.create_ideation_knowledge(
            "ideation-1",
            ideations.IdeationKnowledgeCreate(title="Context", content="Body"),
            user_id="user-1",
            uow=uow,
        ),
        "ideation",
        "ideation-1",
    ),
    (
        ideations.DeleteIdeationKnowledgeUseCase,
        lambda uow: ideations.delete_ideation_knowledge(
            "ideation-1", "knowledge-1", user_id="user-1", uow=uow
        ),
        "ideation",
        "ideation-1",
    ),
    (
        ideations.CreateIdeationQuestionUseCase,
        lambda uow: ideations.create_ideation_question(
            "ideation-1",
            ideations.IdeationQACreate(question="What changed?"),
            user_id="user-1",
            uow=uow,
        ),
        "ideation",
        "ideation-1",
    ),
    (
        ideations.AnswerIdeationQuestionUseCase,
        lambda uow: ideations.answer_ideation_question(
            "ideation-1",
            "qa-1",
            ideations.IdeationQAAnswer(answer="Answer"),
            user_id="user-1",
            uow=uow,
        ),
        "ideation",
        "ideation-1",
    ),
    (
        ideations.DeleteIdeationQuestionUseCase,
        lambda uow: ideations.delete_ideation_question(
            "ideation-1", "qa-1", user_id="user-1", uow=uow
        ),
        "ideation",
        "ideation-1",
    ),
    (
        refinements.UpdateRefinementUseCase,
        lambda uow: refinements.update_refinement(
            "refinement-1",
            refinements.RefinementUpdate(title="Changed"),
            user_id="user-1",
            uow=uow,
        ),
        "refinement",
        "refinement-1",
    ),
    (
        refinements.CreateRefinementKnowledgeUseCase,
        lambda uow: refinements.create_refinement_knowledge(
            "refinement-1",
            refinements.RefinementKnowledgeCreate(title="Context", content="Body"),
            user_id="user-1",
            uow=uow,
        ),
        "refinement",
        "refinement-1",
    ),
    (
        refinements.DeleteRefinementKnowledgeUseCase,
        lambda uow: refinements.delete_refinement_knowledge(
            "refinement-1", "knowledge-1", user_id="user-1", uow=uow
        ),
        "refinement",
        "refinement-1",
    ),
    (
        refinements.CreateRefinementQuestionUseCase,
        lambda uow: refinements.create_refinement_question(
            "refinement-1",
            refinements.RefinementQACreate(question="What changed?"),
            user_id="user-1",
            uow=uow,
        ),
        "refinement",
        "refinement-1",
    ),
    (
        refinements.AnswerRefinementQuestionUseCase,
        lambda uow: refinements.answer_refinement_question(
            "refinement-1",
            "qa-1",
            refinements.RefinementQAAnswer(answer="Answer"),
            user_id="user-1",
            uow=uow,
        ),
        "refinement",
        "refinement-1",
    ),
    (
        refinements.DeleteRefinementQuestionUseCase,
        lambda uow: refinements.delete_refinement_question(
            "refinement-1", "qa-1", user_id="user-1", uow=uow
        ),
        "refinement",
        "refinement-1",
    ),
    (
        specs.UpdateSpecUseCase,
        lambda uow: specs.update_spec(
            "spec-1",
            specs.SpecUpdate(title="Changed"),
            Response(),
            user_id="user-1",
            uow=uow,
        ),
        "spec",
        "spec-1",
    ),
    (
        specs.RunStructuredSpecEntityUseCase,
        lambda uow: specs.create_structured_spec_entity(
            "spec-1",
            "functional_requirement",
            specs.StructuredSpecEntityMutationRequest(payload={"text": "Requirement"}),
            user_id="user-1",
            uow=uow,
        ),
        "spec",
        "spec-1",
    ),
    (
        specs.CreateSpecKnowledgeUseCase,
        lambda uow: specs.create_spec_knowledge(
            "spec-1",
            specs.SpecKnowledgeCreate(title="Context", content="Body"),
            user_id="user-1",
            uow=uow,
        ),
        "spec",
        "spec-1",
    ),
    (
        specs.DeleteSpecKnowledgeUseCase,
        lambda uow: specs.delete_spec_knowledge(
            "spec-1", "knowledge-1", user_id="user-1", uow=uow
        ),
        "spec",
        "spec-1",
    ),
    (
        specs.CreateSpecQuestionUseCase,
        lambda uow: specs.create_spec_question(
            "spec-1",
            specs.SpecQACreate(question="What changed?"),
            user_id="user-1",
            uow=uow,
        ),
        "spec",
        "spec-1",
    ),
    (
        specs.AnswerSpecQuestionUseCase,
        lambda uow: specs.answer_spec_question(
            "spec-1",
            "qa-1",
            specs.SpecQAAnswer(answer="Answer"),
            user_id="user-1",
            uow=uow,
        ),
        "spec",
        "spec-1",
    ),
    (
        specs.DeleteSpecQuestionUseCase,
        lambda uow: specs.delete_spec_question(
            "spec-1", "qa-1", user_id="user-1", uow=uow
        ),
        "spec",
        "spec-1",
    ),
    (
        architecture.CreateArchitectureUseCase,
        lambda uow: architecture._create_architecture(
            "ideation",
            "ideation-1",
            architecture.ArchitectureDesignCreate(
                title="System", global_description="System boundaries"
            ),
            "user-1",
            uow,
        ),
        "ideation",
        "ideation-1",
    ),
    (
        architecture.UpdateArchitectureDesignUseCase,
        lambda uow: architecture.update_architecture_design(
            "architecture-1",
            architecture.ArchitectureDesignUpdate(title="Changed"),
            user_id="user-1",
            uow=uow,
        ),
        "refinement",
        "refinement-1",
    ),
    (
        architecture.DeleteArchitectureDesignUseCase,
        lambda uow: architecture.delete_architecture_design(
            "architecture-1", user_id="user-1", uow=uow
        ),
        "spec",
        "spec-1",
    ),
    (
        architecture.UpdateArchitectureDiagramPayloadUseCase,
        lambda uow: architecture.update_architecture_diagram_payload(
            "architecture-1",
            "diagram-1",
            architecture.DiagramPayloadUpdate(payload={"nodes": []}),
            user_id="user-1",
            uow=uow,
        ),
        "spec",
        "spec-1",
    ),
    (
        architecture.ImportExcalidrawArchitectureDiagramUseCase,
        lambda uow: architecture.import_excalidraw_architecture_diagram(
            "architecture-1",
            architecture.ExcalidrawImportRequest(title="Diagram", payload={}),
            user_id="user-1",
            uow=uow,
        ),
        "spec",
        "spec-1",
    ),
    (
        resource_gate.MarkResourceNotApplicableUseCase,
        lambda uow: resource_gate.mark_resource_not_applicable(
            "spec",
            "spec-1",
            resource_gate.ResourceNotApplicableRequest(
                resource_type="architecture", justification="Not applicable"
            ),
            board_id="board-1",
            user_id="user-1",
            realm_id=None,
            db=uow,
        ),
        "spec",
        "spec-1",
    ),
    (
        resource_gate.ClearResourceNotApplicableUseCase,
        lambda uow: resource_gate.clear_resource_not_applicable(
            "spec",
            "spec-1",
            "architecture",
            resource_gate.ClearResourceNotApplicableRequest(reason="Reopened"),
            board_id="board-1",
            user_id="user-1",
            realm_id=None,
            db=uow,
        ),
        "spec",
        "spec-1",
    ),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("use_case", "invoke", "subject_type", "subject_id"),
    _CASES,
    ids=(
        "ideation-content",
        "ideation-knowledge-create",
        "ideation-knowledge-delete",
        "ideation-question-create",
        "ideation-question-answer",
        "ideation-question-delete",
        "refinement-content",
        "refinement-knowledge-create",
        "refinement-knowledge-delete",
        "refinement-question-create",
        "refinement-question-answer",
        "refinement-question-delete",
        "spec-content",
        "spec-structured-child",
        "spec-knowledge-create",
        "spec-knowledge-delete",
        "spec-question-create",
        "spec-question-answer",
        "spec-question-delete",
        "architecture-create",
        "architecture-update",
        "architecture-delete",
        "architecture-payload-update",
        "architecture-import",
        "resource-not-applicable-mark",
        "resource-not-applicable-clear",
    ),
)
async def test_all_semantic_rest_writes_project_exact_draft_conflict_without_writes(
    monkeypatch,
    use_case: type,
    invoke: RouteInvocation,
    subject_type: str,
    subject_id: str,
) -> None:
    error = SubjectEditRequiresDraftError(subject_type, subject_id, "validation")

    async def reject(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(use_case, "execute", reject)
    uow = _NoWriteUow()

    with pytest.raises(HTTPException) as exc_info:
        await invoke(uow)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == project_subject_edit_requires_draft_error(error)
    assert uow.commit_calls == 0
    assert uow.rollback_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("use_case", "invoke", "subject_type", "subject_id"),
    (
        (
            ideations.MoveIdeationUseCase,
            lambda uow: ideations.move_ideation(
                "ideation-1",
                ideations.IdeationMove(status="review"),
                user_id="user-1",
                uow=uow,
            ),
            "ideation",
            "ideation-1",
        ),
        (
            refinements.MoveRefinementUseCase,
            lambda uow: refinements.move_refinement(
                "refinement-1",
                refinements.RefinementMove(status="review"),
                user_id="user-1",
                uow=uow,
            ),
            "refinement",
            "refinement-1",
        ),
        (
            specs.MoveSpecUseCase,
            lambda uow: specs.move_spec(
                "spec-1",
                specs.SpecMove(status="review"),
                user_id="user-1",
                uow=uow,
            ),
            "spec",
            "spec-1",
        ),
    ),
)
async def test_lifecycle_write_fence_conflict_is_retryable_409(
    monkeypatch,
    use_case: type,
    invoke: RouteInvocation,
    subject_type: str,
    subject_id: str,
) -> None:
    error = LifecycleTransitionConflictError(subject_type, subject_id)

    async def reject(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(use_case, "execute", reject)
    uow = _NoWriteUow()

    with pytest.raises(HTTPException) as exc_info:
        await invoke(uow)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == error.to_error_dict()
    assert exc_info.value.detail["retryable"] is True
    assert uow.commit_calls == 0
    assert uow.rollback_calls == 0
