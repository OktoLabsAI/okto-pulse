"""Community wiring checks for SK-A Quality read contracts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from okto_pulse.community.adapters.sqlalchemy_quality_assessment import (
    CommunitySqlAlchemyQualityAssessment,
    CommunitySqlAlchemyQualityAssessmentPreflightReader,
    _QualitySubjectContext,
    _gate_inputs_for,
)
from okto_pulse.core.domain.quality_assessment import (
    AssessmentSubjectIdentity,
    AssessmentSubjectType,
    QualityPage,
)
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.ports.quality_assessment import (
    AssessmentListQuery,
    AssessmentReadAccessDenied,
    AssessmentReceiptNotFound,
    FindingListQuery,
)
from okto_pulse.core.services.ska_observability import (
    reset_ska_metric_samples_for_tests,
    ska_metric_samples,
)


@pytest.mark.parametrize(
    ("subject_type", "settings", "skipped", "expected"),
    [
        (
            AssessmentSubjectType.IDEATION,
            {
                "require_ideation_ambiguity_gate": True,
                "max_ideation_ambiguity": 2,
            },
            False,
            (True, 2, False),
        ),
        (
            AssessmentSubjectType.REFINEMENT,
            {
                "require_refinement_ambiguity_gate": False,
                "max_refinement_ambiguity": 4,
            },
            True,
            (False, 4, True),
        ),
    ],
)
def test_quality_read_context_projects_real_ambiguity_gate_inputs(
    subject_type: AssessmentSubjectType,
    settings: dict[str, object],
    skipped: bool,
    expected: tuple[bool, int, bool],
) -> None:
    context = _QualitySubjectContext(
        board=SimpleNamespace(settings=settings),
        subject=SimpleNamespace(skip_ambiguity_gate=skipped),
        qa_items=(),
        head=None,
    )

    gate = _gate_inputs_for(
        context=context,
        subject_type=subject_type,
    )[0]

    assert (gate.enabled, gate.threshold, gate.skipped) == expected
    assert gate.applicable is True


def test_quality_read_context_keeps_spec_gate_advisory() -> None:
    context = _QualitySubjectContext(
        board=SimpleNamespace(settings={}),
        subject=SimpleNamespace(skip_ambiguity_gate=True),
        qa_items=(),
        head=None,
    )

    assert (
        _gate_inputs_for(
            context=context,
            subject_type=AssessmentSubjectType.SPEC,
        )
        == ()
    )


@pytest.mark.asyncio
async def test_global_receipt_access_denial_is_indistinguishable_from_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = SimpleNamespace(
        board_id="private-board",
        subject_type="ideation",
        subject_id="private-ideation",
    )

    class _Result:
        def scalar_one_or_none(self):
            return receipt

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _statement):
            return _Result()

    reader = CommunitySqlAlchemyQualityAssessmentPreflightReader(_Session)

    async def denied(**_kwargs):
        raise AssessmentReadAccessDenied()

    monkeypatch.setattr(
        reader,
        "resolve_assessment_read_context",
        denied,
    )

    with pytest.raises(AssessmentReceiptNotFound):
        await reader.resolve_receipt_read_context(
            receipt_id="secret-receipt",
            board_id=None,
            actor_id="outsider",
            realm_scope=RealmScope.local(),
        )


@pytest.mark.asyncio
async def test_quality_sql_pages_emit_bounded_query_observability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = CommunitySqlAlchemyQualityAssessment(SimpleNamespace())

    async def assessment_page(_query):
        return QualityPage(
            items=(),
            total_filtered=0,
            total_overall=0,
            offset=0,
            limit=25,
        )

    async def finding_page(_query):
        return QualityPage(
            items=(),
            total_filtered=0,
            total_overall=0,
            offset=0,
            limit=25,
        )

    monkeypatch.setattr(
        adapter,
        "_list_assessments_unobserved",
        assessment_page,
    )
    monkeypatch.setattr(
        adapter,
        "_list_findings_unobserved",
        finding_page,
    )
    reset_ska_metric_samples_for_tests()

    await adapter.list_assessments(
        AssessmentListQuery(
            subject=AssessmentSubjectIdentity(
                board_id="board-1",
                subject_type=AssessmentSubjectType.IDEATION,
                subject_id="ideation-1",
            ),
            offset=0,
            limit=25,
        )
    )
    await adapter.list_findings(
        FindingListQuery(
            board_id="board-1",
            subject_type=AssessmentSubjectType.IDEATION,
            subject_id="ideation-1",
            offset=0,
            limit=25,
        )
    )

    samples = ska_metric_samples()
    assert [
        (
            sample["surface"],
            sample["subject_type"],
            sample["outcome"],
            sample["value"],
            sample["payload_bytes"],
        )
        for sample in samples
    ] == [
        ("quality_assessments", "ideation", "success", 4, 0),
        ("quality_findings", "ideation", "success", 3, 0),
    ]
