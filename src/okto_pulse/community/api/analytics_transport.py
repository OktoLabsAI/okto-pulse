"""Closed REST DTOs for the canonical A3/A4 analytics projections.

The Core owns the facts and their canonical serialization.  These models keep
the Community OpenAPI surface complete without rebuilding analytics policy in
the transport layer.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel

from okto_pulse.core.domain.code_traceability import (
    CodeEvidenceSpecRelationType,
    CodeEvidenceType,
    CodeTraceabilityLifecycleStatus,
    CodeTraceabilityWaiverEntityType,
    CodeTraceabilityWaiverReason,
    CodeTraceabilityWaiverScope,
    ImplementationTargetExecutionDisposition,
    ImplementationTargetResolutionState,
    TargetOverlapDisposition,
    TargetOverlapSeverity,
)
from okto_pulse.core.ports.coverage_traceability import (
    CodeEvidenceMatrixState,
    CoverageAggregateState,
    CoverageCurrentness,
    CoverageDeliveryState,
    CoverageEvidenceEligibility,
    CoverageFactState,
    CoverageObligationType,
    CoverageSkipState,
)
from okto_pulse.core.ports.flow_health import (
    FlowAuthorityState,
    FlowBlockerCode,
    FlowHealthState,
    FlowLifecycleState,
    FlowSubjectType,
    FlowThresholdProvenance,
)
from okto_pulse.core.ports.delivery_forecast import (
    DeliveryForecastResultState,
    ForecastBacktestState,
    ForecastReadinessState,
)
from okto_pulse.core.ports.board_kg_analytics import (
    BoardKgAnalyticsResultState,
    BoardKgClassificationState,
    BoardKgCognitiveStatus,
    BoardKgDomain,
    BoardKgDomainSeverity,
    BoardKgEffectivenessState,
    BoardKgHealthState,
    BoardKgProvenanceKind,
)
from okto_pulse.core.ports.analytics_provenance import AnalyticsProjectionCurrentness
from okto_pulse.core.models.schemas import FlowHealthSettings


class _CanonicalDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")


AnalyticsScalar = str | int | float | bool | None


class AnalyticsFilterDTO(_CanonicalDTO):
    field: str
    operator: str
    value: AnalyticsScalar | list[AnalyticsScalar]


class AnalyticsExclusionDTO(_CanonicalDTO):
    reason: str
    count: int = Field(ge=0)


class AnalyticsExclusionsDTO(_CanonicalDTO):
    restricted_count: int = Field(ge=0)
    excluded_count: int = Field(ge=0)
    reasons: list[AnalyticsExclusionDTO]


class AnalyticsPopulationScopeDTO(_CanonicalDTO):
    scope_ref: str
    accessible_count: int = Field(ge=0)
    excluded_count: int = Field(ge=0)


class AnalyticsSourceAuthorityDTO(_CanonicalDTO):
    authority: str
    reference: str
    timestamp_field: str


class AnalyticsProjectionProvenanceDTO(_CanonicalDTO):
    observed_at: str
    currentness: AnalyticsProjectionCurrentness
    reason: str | None
    sources: list[AnalyticsSourceAuthorityDTO]


class ForecastDependencyVersionsDTO(_CanonicalDTO):
    analytics_foundation: str
    delivery_phase_1: str


class ForecastReadinessBaseDTO(_CanonicalDTO):
    actual_observations: int = Field(ge=0)
    required_observations: int = Field(ge=1)
    rule_version: str


class ForecastReadyReadinessDTO(ForecastReadinessBaseDTO):
    ready: Literal[True]
    state: Literal[ForecastReadinessState.READY]
    reason: None
    remediation: None


class ForecastNonReadyReadinessDTO(ForecastReadinessBaseDTO):
    ready: Literal[False]
    state: Literal[
        ForecastReadinessState.INSUFFICIENT_HISTORY,
        ForecastReadinessState.UNAVAILABLE,
        ForecastReadinessState.RESTRICTED,
        ForecastReadinessState.EMPTY,
    ]
    reason: str = Field(min_length=1)
    remediation: str = Field(min_length=1)


class ForecastSourcePeriodDTO(_CanonicalDTO):
    from_: str = Field(alias="from")
    to: str


class ForecastEstimateDTO(_CanonicalDTO):
    point: float
    lower_bound: float
    upper_bound: float
    confidence_level: float = Field(gt=0, lt=1)
    horizon: str
    assumptions: list[str] = Field(min_length=1)
    sample_size: int = Field(ge=1)
    source_period: ForecastSourcePeriodDTO
    method_version: str


class ForecastBacktestDTO(_CanonicalDTO):
    state: ForecastBacktestState
    error: float | None
    calibration: float | None
    method_version: str
    sample_size: int = Field(ge=0)
    evaluation_window: ForecastSourcePeriodDTO | None
    reason: str | None


class DeliveryForecastResponseBaseDTO(_CanonicalDTO):
    contract_version: str
    dependency_versions: ForecastDependencyVersionsDTO
    query_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    filters: list[AnalyticsFilterDTO]
    as_of: str
    board_id: str
    result_state: DeliveryForecastResultState
    provenance: AnalyticsProjectionProvenanceDTO
    backtest: ForecastBacktestDTO
    population_scope: AnalyticsPopulationScopeDTO
    exclusions: AnalyticsExclusionsDTO


class DeliveryForecastReadyResponseDTO(DeliveryForecastResponseBaseDTO):
    readiness: ForecastReadyReadinessDTO
    forecast: ForecastEstimateDTO


class DeliveryForecastNonReadyResponseDTO(DeliveryForecastResponseBaseDTO):
    readiness: ForecastNonReadyReadinessDTO


class CanonicalDeliveryForecastResponseDTO(
    RootModel[DeliveryForecastReadyResponseDTO | DeliveryForecastNonReadyResponseDTO]
):
    """Closed ready/non-ready union; non-ready responses cannot contain forecast."""


class CoverageObligationIdentityDTO(_CanonicalDTO):
    spec_id: str
    obligation_type: CoverageObligationType
    obligation_id: str
    edition: int = Field(ge=1)
    currentness: CoverageCurrentness


class CoverageSkipDTO(_CanonicalDTO):
    state: CoverageSkipState
    effective: bool
    authority_ref: str | None
    reason_code: str | None
    currentness: CoverageCurrentness | None


class CoverageEvidenceDTO(_CanonicalDTO):
    evidence_id: str
    evidence_type: CodeEvidenceType
    source_ref: str
    obligation: CoverageObligationIdentityDTO
    relation_type: CodeEvidenceSpecRelationType
    evidence_content_sha256: str
    parent_card_id: str
    delivery_state: CoverageDeliveryState
    lifecycle_status: CodeTraceabilityLifecycleStatus
    currentness: CoverageCurrentness
    currentness_reason: str | None
    authority_ref: str
    eligibility: CoverageEvidenceEligibility


class CoverageObligationDTO(_CanonicalDTO):
    identity: CoverageObligationIdentityDTO
    state: CoverageFactState
    applicable: bool
    covered: bool | None
    skip: CoverageSkipDTO
    authority_ref: str | None
    reason: str | None
    evidence: list[CoverageEvidenceDTO]


class CoverageCountsDTO(_CanonicalDTO):
    state: CoverageAggregateState
    applicable: int | None
    covered: int | None
    uncovered: int | None
    skipped: int | None
    value: float | None
    n: int | None
    reason: str | None


class CoverageTypeDTO(CoverageCountsDTO):
    obligation_type: CoverageObligationType
    rows: list[CoverageObligationDTO]


class CodeEvidenceTargetDTO(_CanonicalDTO):
    target_id: str
    card_id: str
    source_ref: str
    revision: int = Field(ge=1)
    lifecycle_status: CodeTraceabilityLifecycleStatus
    delivery_state: CoverageDeliveryState
    currentness: CoverageCurrentness
    currentness_reason: str | None
    current_resolution_id: str | None


class CodeEvidenceResolutionDTO(_CanonicalDTO):
    resolution_id: str
    target_id: str
    target_revision: int = Field(ge=1)
    state: ImplementationTargetResolutionState
    currentness: CoverageCurrentness
    currentness_reason: str | None
    authority_ref: str


class CodeEvidenceExecutionDTO(_CanonicalDTO):
    execution_id: str
    target_id: str
    target_revision: int = Field(ge=1)
    disposition: ImplementationTargetExecutionDisposition
    currentness: CoverageCurrentness
    currentness_reason: str | None
    authority_ref: str


class CodeEvidenceOverlapDTO(_CanonicalDTO):
    overlap_id: str
    target_a_id: str
    target_b_id: str
    resolution_a_id: str
    resolution_b_id: str
    severity: TargetOverlapSeverity
    disposition: TargetOverlapDisposition | None
    currentness: CoverageCurrentness
    currentness_reason: str | None


class CodeEvidenceWaiverDTO(_CanonicalDTO):
    waiver_id: str
    entity_type: CodeTraceabilityWaiverEntityType
    entity_id: str
    scope: CodeTraceabilityWaiverScope
    reason_code: CodeTraceabilityWaiverReason
    active: bool
    currentness: CoverageCurrentness
    currentness_reason: str | None
    authority_ref: str


class CodeEvidenceMatrixDTO(_CanonicalDTO):
    state: CodeEvidenceMatrixState
    reason: str | None
    targets: list[CodeEvidenceTargetDTO]
    resolutions: list[CodeEvidenceResolutionDTO]
    executions: list[CodeEvidenceExecutionDTO]
    overlaps: list[CodeEvidenceOverlapDTO]
    waivers: list[CodeEvidenceWaiverDTO]


class CanonicalCoverageResponseDTO(_CanonicalDTO):
    contract_version: str
    foundation_version: str
    query_fingerprint: str
    filters: list[AnalyticsFilterDTO]
    as_of: str
    population_scope: AnalyticsPopulationScopeDTO
    exclusions: AnalyticsExclusionsDTO
    evidence_population_scope: AnalyticsPopulationScopeDTO
    evidence_exclusions: AnalyticsExclusionsDTO
    totals: CoverageCountsDTO
    coverage: list[CoverageTypeDTO]
    code_evidence: CodeEvidenceMatrixDTO
    next_cursor: str | None


class FlowPolicyOverrideDTO(_CanonicalDTO):
    state: FlowLifecycleState
    stale_hours: int = Field(ge=1)


class EffectiveFlowPolicyDTO(_CanonicalDTO):
    version: int = Field(ge=1)
    authority_ref: str
    general_stale_hours: int = Field(ge=1)
    rejected_stale_hours: int = Field(ge=1)
    overrides: list[FlowPolicyOverrideDTO]


class EffectiveFlowThresholdDTO(_CanonicalDTO):
    state: FlowLifecycleState
    stale_hours: int = Field(ge=1)
    provenance: FlowThresholdProvenance
    policy_version: int = Field(ge=1)
    authority_ref: str


class FlowSubjectDTO(_CanonicalDTO):
    type: FlowSubjectType
    id: str


class FlowCurrentEpisodeDTO(_CanonicalDTO):
    state: FlowLifecycleState
    entered_at: str
    age_seconds: int = Field(ge=0)
    entry_event_id: str
    authority_ref: str


class FlowReworkEpisodeDTO(_CanonicalDTO):
    attempt: int = Field(ge=1)
    rejected_at: str
    rejection_event_id: str
    rejection_kind: str
    rejection_code: str
    rejection_summary: str
    resumed_at: str | None
    completed_at: str | None


class FlowBlockerDTO(_CanonicalDTO):
    code: FlowBlockerCode
    authority_state: FlowAuthorityState
    authority_ref: str | None
    effective_skip: bool


class FlowHealthItemDTO(_CanonicalDTO):
    subject: FlowSubjectDTO
    state: FlowHealthState
    reason_codes: list[str]
    threshold: EffectiveFlowThresholdDTO | None
    current_episode: FlowCurrentEpisodeDTO | None
    rework: list[FlowReworkEpisodeDTO]
    blockers: list[FlowBlockerDTO]
    source_authority: AnalyticsSourceAuthorityDTO


class FlowHealthSummaryDTO(_CanonicalDTO):
    healthy: int = Field(ge=0)
    at_risk: int = Field(ge=0)
    blocked: int = Field(ge=0)
    stale: int = Field(ge=0)
    restricted: int = Field(ge=0)
    unavailable: int = Field(ge=0)
    inconsistent: int = Field(ge=0)


class CanonicalFlowHealthResponseDTO(_CanonicalDTO):
    contract_version: str
    foundation_version: str
    query_fingerprint: str
    filters: list[AnalyticsFilterDTO]
    as_of: str
    effective_policy: EffectiveFlowPolicyDTO
    population_scope: AnalyticsPopulationScopeDTO
    exclusions: AnalyticsExclusionsDTO
    summary: FlowHealthSummaryDTO
    items: list[FlowHealthItemDTO]
    next_cursor: str | None


class BoardKgHealthComponentDTO(_CanonicalDTO):
    component: str
    health_state: BoardKgHealthState
    result_state: BoardKgAnalyticsResultState
    classification_reason: str


class BoardKgWindowDTO(_CanonicalDTO):
    from_: str = Field(alias="from")
    to: str


class BoardKgQueryDTO(_CanonicalDTO):
    window: BoardKgWindowDTO
    cognitive_status: list[BoardKgCognitiveStatus]
    artifact_types: list[str]
    cursor: str | None
    limit: int = Field(ge=1, le=500)


class BoardKgHealthDTO(_CanonicalDTO):
    state: BoardKgClassificationState
    classification_reason: str
    reason_codes: list[str]
    availability: dict[str, BoardKgAnalyticsResultState]
    components: list[BoardKgHealthComponentDTO]


class BoardKgDomainAgeDTO(_CanonicalDTO):
    result_state: BoardKgAnalyticsResultState
    sample_count: int = Field(ge=0)
    p50_hours: float | None = Field(ge=0)
    p95_hours: float | None = Field(ge=0)
    oldest_hours: float | None = Field(ge=0)
    reason: str | None


class BoardKgDrillDownDTO(_CanonicalDTO):
    allowed: bool
    target: str | None


class BoardKgOperationalDomainDTO(_CanonicalDTO):
    domain: BoardKgDomain
    result_state: BoardKgAnalyticsResultState
    count: int | None = Field(ge=0)
    severity: BoardKgDomainSeverity | None
    age: BoardKgDomainAgeDTO
    drill_down: BoardKgDrillDownDTO
    reason: str | None


class BoardKgCognitiveInventoryDTO(_CanonicalDTO):
    result_state: BoardKgAnalyticsResultState
    by_status: dict[BoardKgCognitiveStatus, int]
    total: int | None = Field(ge=0)
    overdue_revisits: int | None = Field(ge=0)
    age: BoardKgDomainAgeDTO
    reason: str | None


class BoardKgTimingDTO(_CanonicalDTO):
    state: BoardKgEffectivenessState
    sample_count: int = Field(ge=0)
    p50_hours: float | None = Field(ge=0)
    p95_hours: float | None = Field(ge=0)
    reason: str | None


class BoardKgCognitiveEffectivenessDTO(_CanonicalDTO):
    state: BoardKgEffectivenessState
    numerator: int | None = Field(ge=0)
    denominator: int | None = Field(ge=0)
    rate: float | None = Field(ge=0, le=1)
    candidate_count: int | None = Field(ge=0)
    persisted_count: int | None = Field(ge=0)
    conversion_rate: float | None = Field(ge=0, le=1)
    method_version: str
    sample_period: BoardKgWindowDTO
    timing: BoardKgTimingDTO
    reason: str | None


class BoardKgProvenanceValueDTO(_CanonicalDTO):
    count: int = Field(ge=0)
    rate: float | None = Field(ge=0, le=1)


class BoardKgProvenanceMixDTO(_CanonicalDTO):
    result_state: BoardKgAnalyticsResultState
    total: int | None = Field(ge=0)
    by_kind: dict[BoardKgProvenanceKind, BoardKgProvenanceValueDTO]
    reason: str | None


class BoardKgDiagnosticDTO(_CanonicalDTO):
    domain: str
    severity: BoardKgDomainSeverity
    reason: str
    next_step: BoardKgDrillDownDTO


class CanonicalBoardKgAnalyticsResponseDTO(_CanonicalDTO):
    contract_version: str
    foundation_version: str
    query_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    query: BoardKgQueryDTO
    filters: list[AnalyticsFilterDTO]
    as_of: str
    board_id: str
    result_state: BoardKgAnalyticsResultState
    provenance: AnalyticsProjectionProvenanceDTO
    health: BoardKgHealthDTO
    domains: list[BoardKgOperationalDomainDTO]
    cognitive_inventory: BoardKgCognitiveInventoryDTO
    effectiveness: BoardKgCognitiveEffectivenessDTO
    provenance_mix: BoardKgProvenanceMixDTO
    diagnostics: list[BoardKgDiagnosticDTO]
    redactions: list[str]
    population_scope: AnalyticsPopulationScopeDTO
    exclusions: AnalyticsExclusionsDTO
    next_cursor: str | None


class FlowHealthSettingsResponseDTO(_CanonicalDTO):
    board_id: str
    settings: FlowHealthSettings


__all__ = [
    "CanonicalBoardKgAnalyticsResponseDTO",
    "CanonicalCoverageResponseDTO",
    "CanonicalDeliveryForecastResponseDTO",
    "CanonicalFlowHealthResponseDTO",
    "FlowHealthSettingsResponseDTO",
]
