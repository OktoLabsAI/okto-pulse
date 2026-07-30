"""Server-owned SQLAlchemy snapshots for executable guideline policy.

The resolver is deliberately transaction-bound.  Read projections use the
lock-free entry point, while evaluation persistence and lifecycle mutations
use the board -> subject locked entry points.  All facts come from Community
relational authorities; caller-declared booleans and arbitrary evidence blobs
are never accepted as policy facts.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, TypeAlias

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from okto_pulse.core.domain.guideline_compliance import (
    PolicyComplianceCurrentSnapshot,
)
from okto_pulse.core.domain.guideline_policy import (
    GuidelineEnforcement,
    PolicyEntityType,
    PolicyParameter,
    PolicySubjectRef,
    PolicySubjectSnapshot,
)
from okto_pulse.core.domain.guideline_policy_evaluator import (
    POLICY_RULESET_VERSION,
    policy_binding_head_digest_v1,
    policy_evaluation_input_digest_v1,
    policy_set_digest_v1,
)
from okto_pulse.core.domain.guideline_policy_transition import (
    PolicyTransitionSnapshot,
)
from okto_pulse.core.domain.guideline_predicate_catalog import (
    GUIDELINE_PREDICATE_CATALOG_VERSION,
    require_policy_fact,
    validate_policy_fact_value,
)
from okto_pulse.core.domain.quality_assessment import (
    AssessmentDigestSet,
    AssessmentSubjectType,
)
from okto_pulse.core.domain.quality_canonicalization import canonical_sha256
from okto_pulse.core.ports.guideline_policy import (
    GuidelinePolicyDigestConflict,
    GuidelinePolicySubjectConflict,
)
from okto_pulse.core.services.analytics_service import spec_coverage_summary
from okto_pulse.core.services.test_scenario_lifecycle import (
    scenario_has_authenticated_required_evidence,
)
from okto_pulse.core.services.quality_projection_currentness import (
    QualityProjectionCurrentnessError,
    evaluate_quality_projection_currentness,
)
from okto_pulse.core.services.resource_gate import ResourceGateService
from okto_pulse.core.services.resource_gate_contracts import ResourceGateError

from .sqlalchemy_models import (
    Board,
    Card,
    CardDependency,
    Ideation,
    IdeationQAItem,
    PolicyComplianceReceiptRow,
    QualityAssessmentHeadRow,
    QualityAssessmentReceiptRow,
    Refinement,
    RefinementQAItem,
    ResearchDecisionHeadRow,
    Spec,
    Sprint,
)
from .sqlalchemy_policy_subject_versioning import lock_policy_board


ResourceGateReadyResolver: TypeAlias = Callable[
    [str, PolicyEntityType, str],
    bool | Awaitable[bool],
]
UtcClock: TypeAlias = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class _ResolvedSubject:
    snapshot: PolicySubjectSnapshot
    status: str


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value))


def _normalized_labels(value: object) -> tuple[str, ...]:
    if not isinstance(value, tuple | list):
        return ()
    return tuple(
        sorted(
            {item.strip() for item in value if isinstance(item, str) and item.strip()}
        )
    )


def _normalized_ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, tuple | list):
        return ()
    return tuple(
        sorted(
            {item.strip() for item in value if isinstance(item, str) and item.strip()}
        )
    )


def _scenario_id(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    return str(value.get("id") or "").strip()


def _scenario_evidence_count(
    *,
    board_id: str,
    spec: Spec,
    scenario: dict[str, Any],
) -> int:
    """Count only evidence authenticated against the current scenario digest.

    A non-gated scenario without an evidence object must remain zero even
    though the shared lifecycle helper correctly treats its status as not
    requiring evidence.  Evidence V2 claims additionally pass through the
    registered concrete verifier and therefore fail closed when fake or stale.
    """

    evidence = scenario.get("evidence") or scenario.get("latest_evidence")
    if (
        str(scenario.get("status") or "") not in {"automated", "passed", "failed"}
        or not isinstance(evidence, dict)
        or not evidence
    ):
        return 0
    try:
        authenticated = scenario_has_authenticated_required_evidence(
            board_id=board_id,
            spec_id=spec.id,
            scenario=scenario,
            acceptance_criteria=list(spec.acceptance_criteria or ()),
        )
    except (TypeError, ValueError, RuntimeError):
        return 0
    return int(authenticated)


class CommunitySqlAlchemyPolicySubjectSnapshotResolver:
    """Resolve closed-catalog facts and policy currentness from one session."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        resource_gate_ready_resolver: ResourceGateReadyResolver | None = None,
        utc_now: UtcClock | None = None,
    ) -> None:
        self._session = session
        self._resource_gate_ready_resolver = resource_gate_ready_resolver
        self._utc_now = utc_now or (lambda: datetime.now(timezone.utc))

    async def resolve_subject_snapshot(
        self,
        *,
        board_id: str,
        entity_type: PolicyEntityType,
        subject_id: str,
        lock: bool = False,
    ) -> PolicySubjectSnapshot | None:
        """Return a server-owned subject snapshot.

        ``lock=False`` is intended for honest read projections.  Mutation
        callers must use ``resolve_locked_current_snapshot`` or
        ``resolve_transition_snapshot`` so the board mutex precedes the
        refreshed subject row lock.
        """

        resolved = await self._resolve_subject(
            board_id=board_id,
            entity_type=entity_type,
            subject_id=subject_id,
            lock=lock,
        )
        return None if resolved is None else resolved.snapshot

    async def resolve_readonly_current_snapshot(
        self,
        *,
        board_id: str,
        entity_type: PolicyEntityType,
        subject_id: str,
    ) -> PolicyComplianceCurrentSnapshot | None:
        return await self._resolve_current_snapshot(
            board_id=board_id,
            entity_type=entity_type,
            subject_id=subject_id,
            lock=False,
        )

    async def resolve_current_snapshot(
        self,
        *,
        board_id: str,
        entity_type: PolicyEntityType,
        subject_id: str,
    ) -> PolicyComplianceCurrentSnapshot | None:
        """Protocol entry point for lock-free read projections."""

        return await self.resolve_readonly_current_snapshot(
            board_id=board_id,
            entity_type=entity_type,
            subject_id=subject_id,
        )

    async def resolve_locked_current_snapshot(
        self,
        *,
        board_id: str,
        entity_type: PolicyEntityType,
        subject_id: str,
    ) -> PolicyComplianceCurrentSnapshot | None:
        """Resolve under the mutation lock order: board, then subject."""

        return await self._resolve_current_snapshot(
            board_id=board_id,
            entity_type=entity_type,
            subject_id=subject_id,
            lock=True,
        )

    async def resolve_transition_snapshot(
        self,
        *,
        board_id: str,
        entity_type: PolicyEntityType,
        subject_id: str,
        expected_from_status: str,
    ) -> PolicyTransitionSnapshot:
        """Return one transition fence from a single locked transaction view."""

        resolved = await self._resolve_subject(
            board_id=board_id,
            entity_type=entity_type,
            subject_id=subject_id,
            lock=True,
        )
        if resolved is None:
            raise GuidelinePolicySubjectConflict("policy_subject_not_found")
        expected_status = (
            expected_from_status.strip()
            if isinstance(expected_from_status, str)
            else ""
        )
        if not expected_status or resolved.status != expected_status:
            raise GuidelinePolicySubjectConflict(
                "policy_transition_subject_status_conflict",
                details=(
                    ("expected_from_status", expected_status),
                    ("current_status", resolved.status),
                ),
            )
        bindings, revisions = await self._policy_bundle(board_id=board_id)
        applicable_rules = tuple(
            rule
            for revision in revisions
            for rule in revision.rules
            if rule.applies_to(entity_type)
        )
        try:
            current = self._current_from_bundle(
                resolved.snapshot,
                bindings=bindings,
                revisions=revisions,
            )
            receipt = await self._latest_receipt(
                board_id=board_id,
                entity_type=entity_type,
                subject_id=subject_id,
            )
        except (RuntimeError, SQLAlchemyError):
            # Runtime/evaluator availability is decision evidence, not a
            # transport exception. Structural identity and digest conflicts
            # intentionally remain typed failures outside this branch.
            return PolicyTransitionSnapshot(
                board_id=board_id,
                entity_type=entity_type,
                subject_id=subject_id,
                expected_from_status=expected_status,
                applicable_rule_count=len(applicable_rules),
                applicable_blocking_rule_count=sum(
                    rule.enforcement is GuidelineEnforcement.BLOCKING
                    for rule in applicable_rules
                ),
                receipt=None,
                current_snapshot=None,
                evaluation_available=False,
                evaluation_error_code="policy_evaluation_unavailable",
            )
        return PolicyTransitionSnapshot(
            board_id=board_id,
            entity_type=entity_type,
            subject_id=subject_id,
            expected_from_status=expected_status,
            applicable_rule_count=len(applicable_rules),
            applicable_blocking_rule_count=sum(
                rule.enforcement is GuidelineEnforcement.BLOCKING
                for rule in applicable_rules
            ),
            receipt=receipt,
            current_snapshot=current,
        )

    async def _resolve_current_snapshot(
        self,
        *,
        board_id: str,
        entity_type: PolicyEntityType,
        subject_id: str,
        lock: bool,
    ) -> PolicyComplianceCurrentSnapshot | None:
        resolved = await self._resolve_subject(
            board_id=board_id,
            entity_type=entity_type,
            subject_id=subject_id,
            lock=lock,
        )
        if resolved is None:
            return None
        current, _revisions = await self._current_from_subject(resolved.snapshot)
        return current

    async def _current_from_subject(
        self,
        snapshot: PolicySubjectSnapshot,
    ) -> tuple[PolicyComplianceCurrentSnapshot, tuple[Any, ...]]:
        bindings, revisions = await self._policy_bundle(
            board_id=snapshot.subject.board_id
        )
        return (
            self._current_from_bundle(
                snapshot,
                bindings=bindings,
                revisions=revisions,
            ),
            revisions,
        )

    @staticmethod
    def _current_from_bundle(
        snapshot: PolicySubjectSnapshot,
        *,
        bindings: tuple[Any, ...],
        revisions: tuple[Any, ...],
    ) -> PolicyComplianceCurrentSnapshot:
        binding_head_digest = policy_binding_head_digest_v1(bindings)
        policy_set_digest = policy_set_digest_v1(bindings, revisions)
        input_digest = policy_evaluation_input_digest_v1(
            subject_snapshot=snapshot,
            policy_set_digest=policy_set_digest,
            binding_head_digest=binding_head_digest,
        )
        return PolicyComplianceCurrentSnapshot(
            subject=snapshot.subject,
            subject_content_digest=snapshot.content_digest,
            input_digest=input_digest,
            policy_set_digest=policy_set_digest,
            binding_head_digest=binding_head_digest,
            catalog_version=GUIDELINE_PREDICATE_CATALOG_VERSION,
            ruleset_version=POLICY_RULESET_VERSION,
        )

    async def _policy_bundle(
        self,
        *,
        board_id: str,
    ) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
        # Lazy import avoids a module cycle while keeping the existing
        # guideline repository as the sole row-to-domain mapping authority.
        from .sqlalchemy_guideline_policy import (
            CommunitySqlAlchemyGuidelinePolicy,
        )

        policy = CommunitySqlAlchemyGuidelinePolicy(self._session)
        bindings = await policy.list_bindings(board_id=board_id)
        revisions = []
        for binding in bindings:
            revision = await policy.get_revision(
                guideline_id=binding.guideline_id,
                revision_id=binding.revision_id,
            )
            if revision is None:
                raise GuidelinePolicyDigestConflict(
                    "policy_evaluation_bound_revision_missing"
                )
            revisions.append(revision)
        return bindings, tuple(revisions)

    async def _latest_receipt(
        self,
        *,
        board_id: str,
        entity_type: PolicyEntityType,
        subject_id: str,
    ):
        row = (
            await self._session.execute(
                select(PolicyComplianceReceiptRow.receipt_id)
                .where(
                    PolicyComplianceReceiptRow.board_id == board_id,
                    PolicyComplianceReceiptRow.entity_type == entity_type.value,
                    PolicyComplianceReceiptRow.subject_id == subject_id,
                    PolicyComplianceReceiptRow.sealed.is_(True),
                )
                .order_by(
                    PolicyComplianceReceiptRow.evaluated_at.desc(),
                    PolicyComplianceReceiptRow.receipt_id.desc(),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        from .sqlalchemy_guideline_policy import (
            CommunitySqlAlchemyGuidelinePolicy,
        )

        policy = CommunitySqlAlchemyGuidelinePolicy(
            self._session,
            current_snapshot_resolver=self,
        )
        receipt = await policy.get_compliance_receipt(
            board_id=board_id,
            receipt_id=row,
        )
        if receipt is None:
            return None
        evaluated_at = self._utc_now()
        if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
            raise RuntimeError("policy_subject_snapshot_clock_must_be_aware")
        evaluated_at = evaluated_at.astimezone(timezone.utc)
        for rule_result in receipt.rule_results:
            if rule_result.waiver_id is None:
                continue
            authorization = await policy.resolve_effective_waiver(
                board_id=board_id,
                guideline_id=rule_result.guideline_id,
                revision_id=rule_result.revision_id,
                rule_id=rule_result.rule_id,
                entity_type=entity_type,
                subject_id=subject_id,
                subject_version=receipt.subject.subject_version,
                evaluated_at=evaluated_at,
            )
            if (
                authorization is None
                or authorization.waiver.waiver_id != rule_result.waiver_id
            ):
                # The receipt remains historical evidence, but it cannot be
                # used as live transition authority after revoke, expiry,
                # replacement, or source/currentness drift.
                return None
        return receipt

    async def _resolve_subject(
        self,
        *,
        board_id: str,
        entity_type: PolicyEntityType,
        subject_id: str,
        lock: bool,
    ) -> _ResolvedSubject | None:
        if not isinstance(entity_type, PolicyEntityType):
            raise GuidelinePolicySubjectConflict("policy_subject_type_invalid")
        if lock:
            try:
                await lock_policy_board(self._session, board_id=board_id)
            except (TypeError, ValueError, SQLAlchemyError) as exc:
                raise GuidelinePolicySubjectConflict(
                    "policy_subject_board_not_found"
                ) from exc

        if entity_type is PolicyEntityType.TEST_SCENARIO:
            return await self._resolve_test_scenario(
                board_id=board_id,
                subject_id=subject_id,
                lock=lock,
            )

        model_by_type = {
            PolicyEntityType.IDEATION: Ideation,
            PolicyEntityType.REFINEMENT: Refinement,
            PolicyEntityType.SPEC: Spec,
            PolicyEntityType.SPRINT: Sprint,
            PolicyEntityType.CARD: Card,
        }
        model = model_by_type[entity_type]
        statement = (
            select(model)
            .where(model.id == subject_id, model.board_id == board_id)
            .execution_options(populate_existing=True)
        )
        if lock:
            statement = statement.with_for_update()
        row = (await self._session.execute(statement)).scalar_one_or_none()
        if row is None:
            return None
        facts = await self._facts_for_row(
            board_id=board_id,
            entity_type=entity_type,
            row=row,
        )
        version_field = (
            "policy_version" if entity_type is PolicyEntityType.CARD else "version"
        )
        return _ResolvedSubject(
            snapshot=self._snapshot(
                board_id=board_id,
                entity_type=entity_type,
                subject_id=subject_id,
                subject_version=int(getattr(row, version_field)),
                facts=facts,
            ),
            status=_enum_value(row.status),
        )

    async def _resolve_test_scenario(
        self,
        *,
        board_id: str,
        subject_id: str,
        lock: bool,
    ) -> _ResolvedSubject | None:
        statement = (
            select(Spec)
            .where(Spec.board_id == board_id)
            .order_by(Spec.id.asc())
            .execution_options(populate_existing=True)
        )
        if lock:
            statement = statement.with_for_update()
        specs = tuple((await self._session.execute(statement)).scalars().all())
        matches: list[tuple[Spec, dict[str, Any]]] = []
        for spec in specs:
            for scenario in spec.test_scenarios or ():
                if isinstance(scenario, dict) and _scenario_id(scenario) == subject_id:
                    matches.append((spec, scenario))
        if len(matches) > 1:
            raise GuidelinePolicySubjectConflict(
                "policy_test_scenario_subject_duplicate"
            )
        if not matches:
            return None
        spec, scenario = matches[0]
        facts = await self._test_scenario_facts(
            board_id=board_id,
            spec=spec,
            scenario=scenario,
        )
        return _ResolvedSubject(
            snapshot=self._snapshot(
                board_id=board_id,
                entity_type=PolicyEntityType.TEST_SCENARIO,
                subject_id=subject_id,
                subject_version=int(spec.test_scenario_policy_epoch),
                facts=facts,
            ),
            status=str(scenario.get("status") or "").strip(),
        )

    def _snapshot(
        self,
        *,
        board_id: str,
        entity_type: PolicyEntityType,
        subject_id: str,
        subject_version: int,
        facts: tuple[PolicyParameter, ...],
    ) -> PolicySubjectSnapshot:
        validated = tuple(
            (
                code,
                validate_policy_fact_value(
                    require_policy_fact(entity_type, code),
                    value,
                ),
            )
            for code, value in facts
        )
        subject = PolicySubjectRef(
            board_id=board_id,
            entity_type=entity_type,
            subject_id=subject_id,
            subject_version=subject_version,
        )
        content_digest = canonical_sha256(
            {
                "contract": "policy-subject-snapshot/v1",
                "subject": {
                    "board_id": board_id,
                    "entity_type": entity_type.value,
                    "subject_id": subject_id,
                    "subject_version": subject_version,
                },
                "facts": validated,
            }
        )
        return PolicySubjectSnapshot(
            subject=subject,
            content_digest=content_digest,
            captured_at=datetime.now(timezone.utc),
            attributes=validated,
        )

    async def _facts_for_row(
        self,
        *,
        board_id: str,
        entity_type: PolicyEntityType,
        row: Any,
    ) -> tuple[PolicyParameter, ...]:
        facts: list[PolicyParameter] = [
            ("status", _enum_value(row.status)),
            (
                "resource_gate_ready",
                await self._resource_gate_ready(
                    board_id=board_id,
                    entity_type=entity_type,
                    subject_id=row.id,
                ),
            ),
        ]
        labels = _normalized_labels(getattr(row, "labels", None))
        if labels:
            facts.append(("labels", labels))

        if entity_type is PolicyEntityType.IDEATION:
            if row.complexity is not None:
                facts.append(("complexity", _enum_value(row.complexity)))
            open_qa = (
                await self._session.execute(
                    select(func.count())
                    .select_from(IdeationQAItem)
                    .where(
                        IdeationQAItem.ideation_id == row.id,
                        IdeationQAItem.answered_at.is_(None),
                        IdeationQAItem.tombstoned.is_(False),
                        IdeationQAItem.lifecycle == "active",
                    )
                )
            ).scalar_one()
            facts.append(("qa_open_count", int(open_qa)))
            ambiguity = await self._current_ambiguity_score(
                board_id=board_id,
                subject_type=AssessmentSubjectType.IDEATION,
                subject=row,
                qa_model=IdeationQAItem,
                qa_fk=IdeationQAItem.ideation_id,
            )
            if ambiguity is not None:
                facts.append(("ambiguity_score", ambiguity))

        elif entity_type is PolicyEntityType.REFINEMENT:
            statuses = tuple(
                (
                    await self._session.execute(
                        select(ResearchDecisionHeadRow.status).where(
                            ResearchDecisionHeadRow.board_id == board_id,
                            ResearchDecisionHeadRow.refinement_id == row.id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            facts.extend(
                (
                    (
                        "research_open_count",
                        sum(status != "resolved" for status in statuses),
                    ),
                    (
                        "research_resolved_count",
                        sum(status == "resolved" for status in statuses),
                    ),
                )
            )
            ambiguity = await self._current_ambiguity_score(
                board_id=board_id,
                subject_type=AssessmentSubjectType.REFINEMENT,
                subject=row,
                qa_model=RefinementQAItem,
                qa_fk=RefinementQAItem.refinement_id,
            )
            if ambiguity is not None:
                facts.append(("ambiguity_score", ambiguity))

        elif entity_type is PolicyEntityType.SPEC:
            cards = tuple(
                (
                    await self._session.execute(
                        select(Card).where(
                            Card.board_id == board_id,
                            Card.spec_id == row.id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            coverage = spec_coverage_summary(row, cards=list(cards))
            facts.extend(
                (
                    ("fr_count", len(row.functional_requirements or ())),
                    ("ac_count", len(row.acceptance_criteria or ())),
                    ("tr_count", len(row.technical_requirements or ())),
                    ("test_scenario_count", len(row.test_scenarios or ())),
                    (
                        "coverage_percent",
                        float(coverage["ac_coverage_pct"]),
                    ),
                    ("validation_state", self._validation_state(row)),
                )
            )

        elif entity_type is PolicyEntityType.SPRINT:
            cards = tuple(
                (
                    await self._session.execute(
                        select(Card).where(
                            Card.board_id == board_id,
                            Card.sprint_id == row.id,
                            Card.archived.is_(False),
                        )
                    )
                )
                .scalars()
                .all()
            )
            spec = await self._load_spec(
                board_id=board_id,
                spec_id=row.spec_id,
            )
            scenario_by_id = (
                {
                    _scenario_id(item): item
                    for item in spec.test_scenarios or ()
                    if isinstance(item, dict) and _scenario_id(item)
                }
                if spec is not None and spec.board_id == board_id
                else {}
            )
            scoped = tuple(
                scenario_by_id[scenario_id]
                for scenario_id in _normalized_ids(row.test_scenario_ids)
                if scenario_id in scenario_by_id
            )
            facts.extend(
                (
                    ("card_count", len(cards)),
                    (
                        "open_card_count",
                        sum(
                            _enum_value(card.status) not in {"done", "cancelled"}
                            for card in cards
                        ),
                    ),
                    ("test_scenario_count", len(scoped)),
                    (
                        "passed_scenario_count",
                        sum(
                            str(item.get("status") or "") == "passed" for item in scoped
                        ),
                    ),
                )
            )

        elif entity_type is PolicyEntityType.CARD:
            upstream = aliased(Card)
            dependencies = tuple(
                (
                    await self._session.execute(
                        select(upstream)
                        .join(
                            CardDependency,
                            CardDependency.depends_on_id == upstream.id,
                        )
                        .where(
                            CardDependency.card_id == row.id,
                            upstream.board_id == board_id,
                            upstream.archived.is_(False),
                        )
                    )
                )
                .scalars()
                .all()
            )
            spec = (
                await self._load_spec(
                    board_id=board_id,
                    spec_id=row.spec_id,
                )
                if row.spec_id is not None
                else None
            )
            scenario_by_id = (
                {
                    _scenario_id(item): item
                    for item in spec.test_scenarios or ()
                    if isinstance(item, dict) and _scenario_id(item)
                }
                if spec is not None and spec.board_id == board_id
                else {}
            )
            linked = tuple(
                scenario_by_id[scenario_id]
                for scenario_id in _normalized_ids(row.test_scenario_ids)
                if scenario_id in scenario_by_id
            )
            facts.extend(
                (
                    ("card_type", _enum_value(row.card_type)),
                    ("priority", _enum_value(row.priority)),
                    (
                        "dependency_open_count",
                        sum(
                            _enum_value(dependency.status) not in {"done", "cancelled"}
                            for dependency in dependencies
                        ),
                    ),
                    ("test_scenario_count", len(linked)),
                    (
                        "evidence_count",
                        (
                            sum(
                                _scenario_evidence_count(
                                    board_id=board_id,
                                    spec=spec,
                                    scenario=scenario,
                                )
                                for scenario in linked
                            )
                            if spec is not None
                            else 0
                        ),
                    ),
                )
            )
        return tuple(facts)

    async def _test_scenario_facts(
        self,
        *,
        board_id: str,
        spec: Spec,
        scenario: dict[str, Any],
    ) -> tuple[PolicyParameter, ...]:
        scenario_id = _scenario_id(scenario)
        linked_cards = tuple(
            (
                await self._session.execute(
                    select(Card).where(
                        Card.board_id == board_id,
                        Card.spec_id == spec.id,
                        Card.archived.is_(False),
                    )
                )
            )
            .scalars()
            .all()
        )
        facts: list[PolicyParameter] = [
            ("status", str(scenario.get("status") or "").strip()),
            ("resource_gate_ready", False),
        ]
        labels = _normalized_labels(scenario.get("labels"))
        if labels:
            facts.append(("labels", labels))
        facts.extend(
            (
                (
                    "scenario_type",
                    str(
                        scenario.get("scenario_type")
                        or scenario.get("type")
                        or "integration"
                    ).strip(),
                ),
                (
                    "linked_test_card_count",
                    sum(
                        _enum_value(card.card_type) == "test"
                        and scenario_id in _normalized_ids(card.test_scenario_ids)
                        for card in linked_cards
                    ),
                ),
                (
                    "evidence_count",
                    _scenario_evidence_count(
                        board_id=board_id,
                        spec=spec,
                        scenario=scenario,
                    ),
                ),
            )
        )
        return tuple(facts)

    async def _load_spec(
        self,
        *,
        board_id: str,
        spec_id: str,
    ) -> Spec | None:
        return (
            await self._session.execute(
                select(Spec)
                .where(
                    Spec.id == spec_id,
                    Spec.board_id == board_id,
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

    @staticmethod
    def _validation_state(spec: Spec) -> str:
        current_id = str(spec.current_validation_id or "").strip()
        if not current_id:
            return "not_validated"
        matches = tuple(
            item
            for item in spec.validations or ()
            if isinstance(item, dict)
            and str(item.get("id") or "").strip() == current_id
        )
        if len(matches) != 1:
            return "validation_unavailable"
        outcome = str(matches[0].get("outcome") or "").strip()
        return outcome or "validation_unavailable"

    async def _current_ambiguity_score(
        self,
        *,
        board_id: str,
        subject_type: AssessmentSubjectType,
        subject: Any,
        qa_model: type,
        qa_fk: Any,
    ) -> float | None:
        head = (
            await self._session.execute(
                select(QualityAssessmentHeadRow).where(
                    QualityAssessmentHeadRow.board_id == board_id,
                    QualityAssessmentHeadRow.subject_type == subject_type.value,
                    QualityAssessmentHeadRow.subject_id == subject.id,
                    QualityAssessmentHeadRow.assessment_kind == "ambiguity",
                )
            )
        ).scalar_one_or_none()
        if head is None:
            return None
        receipt = await self._session.get(
            QualityAssessmentReceiptRow,
            head.receipt_id,
        )
        if receipt is None:
            return None
        qa_items = tuple(
            (
                await self._session.execute(
                    select(qa_model)
                    .where(qa_fk == subject.id)
                    .order_by(qa_model.id.asc())
                )
            )
            .scalars()
            .all()
        )
        board = (
            await self._session.execute(
                select(Board)
                .where(Board.id == board_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if board is None:
            return None
        try:
            assessed_digests = AssessmentDigestSet(
                content_digest=receipt.content_digest,
                clarification_digest=receipt.clarification_digest,
                ruleset_digest=receipt.ruleset_digest,
                taxonomy_digest=receipt.taxonomy_digest,
                policy_digest=receipt.policy_digest,
                input_digest=receipt.input_digest,
                canonicalization_version=receipt.canonicalization_version,
            )
            currentness = evaluate_quality_projection_currentness(
                board_id=board_id,
                subject_type=subject_type,
                subject_id=subject.id,
                assessed_subject_version=receipt.subject_version,
                assessed_digests=assessed_digests,
                assessment_kind=receipt.assessment_kind,
                origin=receipt.origin,
                source=receipt.source,
                current_subject=subject,
                qa_items=qa_items,
                board_settings=board.settings,
            )
        except (QualityProjectionCurrentnessError, TypeError, ValueError):
            return None
        return float(receipt.score) if currentness.current else None

    async def _resource_gate_ready(
        self,
        *,
        board_id: str,
        entity_type: PolicyEntityType,
        subject_id: str,
    ) -> bool:
        if entity_type not in {
            PolicyEntityType.IDEATION,
            PolicyEntityType.REFINEMENT,
            PolicyEntityType.SPEC,
            PolicyEntityType.CARD,
        }:
            return False
        if self._resource_gate_ready_resolver is not None:
            value = self._resource_gate_ready_resolver(
                board_id,
                entity_type,
                subject_id,
            )
            if hasattr(value, "__await__"):
                value = await value  # type: ignore[misc]
            return bool(value)
        try:
            result = await ResourceGateService(
                self._session
            ).validate_entity_completion(
                board_id,
                entity_type.value,
                subject_id,
            )
        except (ResourceGateError, RuntimeError, SQLAlchemyError):
            return False
        return bool(result.get("allowed"))


__all__ = ["CommunitySqlAlchemyPolicySubjectSnapshotResolver"]
