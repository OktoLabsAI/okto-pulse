/**
 * Closed frontend contract for SK-B policy governance.
 *
 * These types intentionally do not extend the legacy Guideline or the SK-A
 * QualityAssessment contracts. Policy governance has its own versioned
 * identity, evidence, currentness, and waiver semantics.
 */

export type PolicyProjection = 'summary' | 'detail' | 'full';

export type PolicyEntityType =
  | 'ideation'
  | 'refinement'
  | 'spec'
  | 'sprint'
  | 'card'
  | 'test_scenario';

export type PolicyGuidelineScope = 'global' | 'inline';
export type GuidelineContextScope = 'all';
export type GuidelineEnforcement = 'advisory' | 'blocking';
export type GuidelineBindingState = 'active' | 'unlinked';
export type GuidelineBindingProvenance =
  | 'native'
  | 'default_materialization';
export type GuidelineBindingMaterialization = 'live' | 'candidate';
export type GuidelineLifecycleStatus = 'retired' | 'superseded';
export type GuidelineVersionBump = 'patch' | 'minor' | 'major';
export type GuidelineImpactItemKind =
  | 'binding'
  | 'target'
  | 'artifact'
  | 'waiver';

export type PolicyCurrentness = 'current' | 'stale';
export type PolicyTransitionReasonCode =
  | 'transition_not_allowed'
  | 'policy_compliance_not_required'
  | 'policy_compliance_receipt_missing'
  | 'policy_compliance_receipt_stale'
  | 'policy_compliance_blocked'
  | 'policy_assessment_unavailable'
  | 'policy_compliance_ready'
  | 'policy_compliance_ready_with_waivers'
  | 'policy_compliance_not_applicable'
  | 'policy_compliance_advisory_only'
  | 'policy_subject_required';

export type PolicyTransitionDiagnosticCode =
  | 'policy_compliance_receipt_missing'
  | 'policy_compliance_receipt_stale'
  | 'policy_assessment_unavailable'
  | 'policy_assessment_inadmissible'
  | 'policy_metric_threshold_failed';

export type SemanticAssessmentInadmissibilityCause =
  | 'confidence_below_minimum'
  | 'assessor_separation_required';

export interface PolicyComplianceBindingDecision {
  binding_id: string;
  guideline_id: string;
  enforcement: GuidelineEnforcement;
  applicable_metric_count: number;
  allowed: boolean;
  assessment_available: boolean;
  receipt_id: string | null;
  currentness: PolicyCurrentness | null;
  currentness_reasons: SemanticAssessmentCurrentnessReason[];
  inadmissibility_cause: SemanticAssessmentInadmissibilityCause | null;
  failed_metric_count: number;
  waived_metric_count: number;
  blocking_metric_count: number;
  advisory_issue_count: number;
  skipped: boolean;
  diagnostic_codes: PolicyTransitionDiagnosticCode[];
}

export interface PolicyComplianceTransitionDecision {
  state: PolicyTransitionReasonCode;
  allowed: boolean | null;
  policy_compliance_required: boolean;
  reason_codes: PolicyTransitionReasonCode[];
  decision_digest: string | null;
  fence_digest: string | null;
  receipt_ids: string[];
  currentness: PolicyCurrentness | null;
  currentness_reasons: SemanticAssessmentCurrentnessReason[];
  applicable_metric_count: number | null;
  applicable_blocking_metric_count: number | null;
  failed_metric_count: number | null;
  blocking_metric_count: number | null;
  waived_metric_count: number | null;
  advisory_issue_count: number | null;
  skipped_binding_count: number | null;
  diagnostic_codes: PolicyTransitionDiagnosticCode[];
  binding_decisions: PolicyComplianceBindingDecision[];
}

export type PolicyWaiverStatus =
  | 'requested'
  | 'approved'
  | 'rejected'
  | 'revoked'
  | 'expired';
export type PolicyWaiverReviewDecision = 'approve' | 'reject';
export type PolicyWaiverEventType =
  | 'request'
  | 'approve'
  | 'reject'
  | 'revoke'
  | 'expire'
  | 'revalidate';
export type NonEmptyArray<T> = [T, ...T[]];

export type GuidelineMetricDirection = 'minimum' | 'maximum';

export interface GuidelineMetricInput {
  metric_id: string;
  code: string;
  title: string;
  description: string;
  evaluation_rubric: string;
  target_entity_types: NonEmptyArray<PolicyEntityType>;
  direction: GuidelineMetricDirection;
  default_threshold: number;
}

export type GuidelineMetric = GuidelineMetricInput;

export type GuidelineMetricThresholdOverrides = Record<string, number>;

export interface GuidelineMetricAssessmentResult {
  metric_id: string;
  score: number;
  rationale: string;
  evidence_refs: string[];
  pinpoints: string[];
}

export interface GuidelineSemanticAssessment {
  confidence: number;
  metric_results: GuidelineMetricAssessmentResult[];
}

export type SemanticAssessmentOutcome =
  | 'passed'
  | 'metric_threshold_failed';
export type SemanticMetricOutcome = 'pass' | 'fail';
export type SemanticThresholdSource = 'default' | 'override';
export type SemanticAssessmentCurrentnessReason =
  | 'current_snapshot_missing'
  | 'subject_version_changed'
  | 'subject_content_changed'
  | 'guideline_revision_changed'
  | 'guideline_revision_digest_changed'
  | 'binding_revision_changed'
  | 'binding_configuration_changed'
  | 'policy_set_changed'
  | 'binding_head_changed'
  | 'input_digest_changed';

export interface SemanticEvidenceRef {
  source_type: string;
  source_id: string;
  source_version: number;
  content_hash: string;
}

export type SemanticPinpointAnchorType =
  | 'whole_artifact'
  | 'field'
  | 'structured_child'
  | 'qa';

export interface SemanticPinpoint {
  anchor_type: SemanticPinpointAnchorType;
  anchor_ref: string | null;
  excerpt_hash: string | null;
  input_digest: string;
}

export interface SemanticMetricResultDetail {
  metric_result_id: string;
  metric_id: string;
  metric_code: string;
  score: number;
  direction: GuidelineMetricDirection;
  default_threshold: number;
  effective_threshold: number;
  threshold_source: SemanticThresholdSource;
  outcome: SemanticMetricOutcome;
  rationale: string;
  evidence_refs: SemanticEvidenceRef[];
  pinpoints: SemanticPinpoint[];
}

export interface SemanticMetricResultFull
  extends SemanticMetricResultDetail {
  metric_definition_digest: string;
}

interface SemanticAssessmentBase {
  receipt_id: string;
  board_id: string;
  entity_type: PolicyEntityType;
  subject_id: string;
  subject_version: number;
  binding_id: string;
  guideline_id: string;
  guideline_revision_id: string;
  enforcement: GuidelineEnforcement;
  state: SemanticAssessmentOutcome;
  currentness: PolicyCurrentness;
  currentness_reasons: SemanticAssessmentCurrentnessReason[];
  confidence: number;
  minimum_confidence: number;
  metric_count: number;
  failed_metric_count: number;
  recorded_at: string;
}

export interface SemanticAssessmentSummary
  extends SemanticAssessmentBase {
  projection: 'summary';
}

export interface SemanticAssessmentDetail
  extends SemanticAssessmentBase {
  projection: 'detail';
  binding_revision: number;
  assessor_agent_id: string;
  assessor_model_id: string | null;
  assessor_independent: boolean;
  confidence_admissible: boolean;
  metric_results: SemanticMetricResultDetail[];
}

export interface SemanticAssessmentFull
  extends Omit<SemanticAssessmentDetail, 'projection' | 'metric_results'> {
  projection: 'full';
  metric_results: SemanticMetricResultFull[];
  subject_content_digest: string;
  last_semantic_editor_id: string;
  guideline_revision_digest: string;
  binding_configuration_digest: string;
  policy_set_digest: string;
  binding_head_digest: string;
  input_digest: string;
  request_digest: string;
  idempotency_key: string;
  receipt_digest: string;
}

export type SemanticAssessmentListItem =
  | SemanticAssessmentSummary
  | SemanticAssessmentDetail
  | SemanticAssessmentFull;

interface SemanticFindingBase {
  finding_id: string;
  receipt_id: string;
  board_id: string;
  entity_type: PolicyEntityType;
  subject_id: string;
  subject_version: number;
  guideline_id: string;
  guideline_revision_id: string;
  binding_id: string;
  metric_id: string;
  metric_code: string;
  currentness: PolicyCurrentness;
  currentness_reasons: SemanticAssessmentCurrentnessReason[];
  created_at: string;
}

export interface SemanticFindingSummary extends SemanticFindingBase {
  projection: 'summary';
}

export interface SemanticFindingDetail extends SemanticFindingBase {
  projection: 'detail';
  metric_result_id: string;
  binding_revision: number;
  rationale: string;
  evidence_refs: SemanticEvidenceRef[];
  pinpoints: SemanticPinpoint[];
}

export interface SemanticFindingFull
  extends Omit<SemanticFindingDetail, 'projection'> {
  projection: 'full';
  metric_result_digest: string;
  receipt_digest: string;
  subject_content_digest: string;
  guideline_revision_digest: string;
  binding_configuration_digest: string;
  finding_digest: string;
}

export type SemanticFindingListItem =
  | SemanticFindingSummary
  | SemanticFindingDetail
  | SemanticFindingFull;

export type SemanticMetricWaiverExpireReason =
  | 'scheduled_expiry'
  | 'subject_scope_changed'
  | 'guideline_revision_changed'
  | 'binding_configuration_changed'
  | 'metric_result_changed';
export type SemanticMetricWaiverRevalidationStatus =
  | 'approved'
  | 'expired'
  | 'anchor_stale'
  | 'revoked';
export type SemanticMetricWaiverRevalidationReason =
  | 'current'
  | 'scheduled_expiry'
  | 'anchor_missing'
  | 'subject_scope_changed'
  | 'guideline_revision_changed'
  | 'binding_configuration_changed'
  | 'metric_result_changed'
  | 'revoked';

interface SemanticWaiverBase {
  waiver_id: string;
  board_id: string;
  entity_type: PolicyEntityType;
  subject_id: string;
  subject_version: number;
  finding_id: string;
  receipt_id: string;
  guideline_id: string;
  guideline_revision_id: string;
  binding_id: string;
  metric_id: string;
  metric_code: string;
  status: PolicyWaiverStatus;
  waiver_revision: number;
  currentness: PolicyCurrentness;
  currentness_reasons: SemanticAssessmentCurrentnessReason[];
  requested_at: string;
  expires_at: string | null;
  last_event_type: PolicyWaiverEventType;
  last_event_at: string;
}

export interface SemanticWaiverSummary extends SemanticWaiverBase {
  projection: 'summary';
}

export interface SemanticWaiverDetail extends SemanticWaiverBase {
  projection: 'detail';
  justification: string;
  requested_by: string;
  original_expires_at: string | null;
  reviewed_by: string | null;
  reviewed_at: string | null;
  review_reason: string | null;
  revoked_by: string | null;
  revoked_at: string | null;
  expire_reason: SemanticMetricWaiverExpireReason | null;
  evidence_refs: SemanticEvidenceRef[];
}

export interface SemanticWaiverFull
  extends Omit<SemanticWaiverDetail, 'projection'> {
  projection: 'full';
  metric_result_id: string;
  metric_result_digest: string;
  finding_digest: string;
  receipt_digest: string;
  subject_content_digest: string;
  guideline_revision_digest: string;
  binding_revision: number;
  binding_configuration_digest: string;
  scope_digest: string;
  head_digest: string;
  last_event_id: string;
  last_event_idempotency_key: string;
  assessment_assessor_id: string;
  last_revalidation_status: SemanticMetricWaiverRevalidationStatus | null;
  last_revalidation_current: boolean | null;
  last_revalidation_reason_code:
    | SemanticMetricWaiverRevalidationReason
    | null;
  last_revalidation_evaluated_at: string | null;
  last_revalidation_currentness_reasons:
    SemanticAssessmentCurrentnessReason[];
  last_revalidation_scheduled_expiry_observed: boolean;
}

export type SemanticWaiverListItem =
  | SemanticWaiverSummary
  | SemanticWaiverDetail
  | SemanticWaiverFull;

export type SemanticSkipStatus = 'active' | 'revoked';
export type SemanticSkipEventType = 'create' | 'revoke';

interface SemanticSkipBase {
  skip_id: string;
  board_id: string;
  entity_type: PolicyEntityType;
  subject_id: string;
  subject_version: number;
  guideline_id: string;
  guideline_revision_id: string;
  binding_id: string;
  status: SemanticSkipStatus;
  skip_revision: number;
  currentness: PolicyCurrentness;
  currentness_reasons: SemanticAssessmentCurrentnessReason[];
  created_at: string;
  last_event_type: SemanticSkipEventType;
  last_event_at: string;
}

export interface SemanticSkipSummary extends SemanticSkipBase {
  projection: 'summary';
}

export interface SemanticSkipDetail extends SemanticSkipBase {
  projection: 'detail';
  binding_revision: number;
  reason: string;
  created_by: string;
  revoked_by: string | null;
  revoked_at: string | null;
  revocation_reason: string | null;
}

export interface SemanticSkipFull
  extends Omit<SemanticSkipDetail, 'projection'> {
  projection: 'full';
  subject_content_digest: string;
  guideline_revision_digest: string;
  binding_configuration_digest: string;
  scope_digest: string;
  last_event_id: string;
  idempotency_key: string;
  request_digest: string;
  skip_digest: string;
}

export type SemanticSkipListItem =
  | SemanticSkipSummary
  | SemanticSkipDetail
  | SemanticSkipFull;

export interface SemanticCursorPage<T> {
  items: T[];
  projection: PolicyProjection;
  next_cursor: string | null;
  has_more: boolean;
}

export interface SemanticAssessmentResponse {
  assessment: SemanticAssessmentListItem;
}

export type SemanticAssessmentContractVersion = 'v1' | 'v2';
export type SemanticPinpointKind = 'evidence' | 'issue';
export type SemanticPinpointSeverity = 'low' | 'medium' | 'high' | 'critical';
export type SemanticAnchorAvailability =
  | 'available'
  | 'removed'
  | 'inaccessible';

export interface SemanticAnchorV2 {
  anchor_type: SemanticPinpointAnchorType;
  anchor_ref: string | null;
  excerpt_hash: string | null;
}

export interface SemanticAnchorSnapshotV2 {
  label: string;
  excerpt: string | null;
  source_version: string;
  availability_at_seal: SemanticAnchorAvailability;
}

export interface SemanticPinpointV2 {
  contract_version: 'v2';
  pinpoint_key: string;
  kind: SemanticPinpointKind;
  title: string;
  detail: string;
  severity: SemanticPinpointSeverity | null;
  remediation: string | null;
  anchor: SemanticAnchorV2;
  anchor_snapshot: SemanticAnchorSnapshotV2;
  blocking: boolean;
}

export interface SemanticMetricResultV2 {
  metric_result_id: string;
  metric_result_digest: string;
  metric_id: string;
  metric_code: string;
  score: number;
  direction: GuidelineMetricDirection;
  default_threshold: number;
  effective_threshold: number;
  threshold_source: SemanticThresholdSource;
  outcome: SemanticMetricOutcome;
  blocking: boolean;
  pinpoints: SemanticPinpointV2[];
}

export interface SemanticAssessmentCurrentV2 {
  receipt_id: string;
  receipt_digest: string;
  currentness: 'current';
  board_id: string;
  subject_type: PolicyEntityType;
  subject_id: string;
  subject_version: number;
  binding_id: string;
  guideline_id: string;
  guideline_revision_id: string;
  confidence: number;
  recorded_at: string;
  metrics: SemanticMetricResultV2[];
}

export type SemanticCurrentAssessmentResponse =
  | {
      contract_version: 'v1';
      assessment: SemanticAssessmentDetail;
    }
  | {
      contract_version: 'v2';
      assessment: SemanticAssessmentCurrentV2;
    };

export interface SemanticWaiverResponse {
  waiver: SemanticWaiverListItem;
}

export interface SemanticSkipResponse {
  skip: SemanticSkipListItem;
}

export interface PolicyGuidelineRoot {
  guideline_id: string;
  owner_id: string;
  scope: PolicyGuidelineScope;
  created_at: string;
  board_id: string | null;
  context_scope: GuidelineContextScope;
}

export interface GuidelineRevision {
  revision_id: string;
  guideline_id: string;
  revision_number: number;
  semantic_version: string;
  title: string;
  content: string;
  revision_digest: string;
  metrics: GuidelineMetric[];
  created_by: string;
  created_at: string;
  parent_revision_id: string | null;
  tags: string[];
}

export interface GuidelineHead {
  guideline_id: string;
  revision_id: string;
  revision_number: number;
  semantic_version: string;
  head_revision: number;
  updated_at: string;
}

export interface GuidelineRetirement {
  retirement_id: string;
  guideline_id: string;
  status: GuidelineLifecycleStatus;
  retired_revision_id: string;
  retired_revision_number: number;
  retired_semantic_version: string;
  retired_revision_digest: string;
  retired_head_revision: number;
  reason: string;
  retired_by: string;
  retired_at: string;
  superseded_by_guideline_id: string | null;
}

export interface BoardGuidelineBinding {
  binding_id: string;
  board_id: string;
  guideline_id: string;
  revision_id: string;
  semantic_version: string;
  revision_digest: string;
  priority: number;
  binding_revision: number;
  adopted_by: string;
  adopted_at: string;
  enforcement: GuidelineEnforcement;
  minimum_confidence: number;
  metric_threshold_overrides: GuidelineMetricThresholdOverrides;
  state: GuidelineBindingState;
  source_kind: GuidelineBindingProvenance;
}

interface GuidelineRevisionListItemBase {
  revision_id: string;
  guideline_id: string;
  revision_number: number;
  semantic_version: string;
  title: string;
  created_by: string;
  created_at: string;
  parent_revision_id?: string;
}

export interface GuidelineRevisionSummary
  extends GuidelineRevisionListItemBase {
  projection: 'summary';
  content?: never;
  revision_digest?: never;
  tags?: never;
  metrics?: never;
}

export interface GuidelineRevisionDetail
  extends GuidelineRevisionListItemBase {
  projection: 'detail';
  content: string;
  revision_digest: string;
  tags: string[];
  metrics: GuidelineMetric[];
}

export type GuidelineRevisionListItem =
  | GuidelineRevisionSummary
  | GuidelineRevisionDetail;

interface PolicyCursorPageBase<T> {
  items: T[];
  limit: number;
}

export type PolicyCursorPage<T> =
  | (PolicyCursorPageBase<T> & {
      has_more: true;
      next_cursor: string;
    })
  | (PolicyCursorPageBase<T> & {
      has_more: false;
      next_cursor?: never;
    });

type OmitNullAsOptional<T, K extends keyof T> =
  & Omit<T, K>
  & {
    [P in K]?: Exclude<T[P], null>;
  };

export interface GuidelineRevisionAuthorityResponse {
  guideline: OmitNullAsOptional<PolicyGuidelineRoot, 'board_id'>;
  revision: OmitNullAsOptional<GuidelineRevision, 'parent_revision_id'>;
  head: GuidelineHead;
  retirement?: OmitNullAsOptional<
    GuidelineRetirement,
    'superseded_by_guideline_id'
  >;
}

export interface GuidelineRevisionContentInput {
  title: string;
  body: string;
}

export interface CreateGuidelineRevisionRequest {
  expected_head_revision: number;
  version_bump: GuidelineVersionBump;
  content: GuidelineRevisionContentInput;
  metrics: GuidelineMetricInput[];
}

export interface CreateGuidelineRevisionResponse {
  revision_id: string;
  revision: string;
  revision_digest: string;
  metrics: GuidelineMetric[];
}

interface RetireGuidelineRequestBase {
  retirement_id: string;
  reason: string;
  idempotency_key: string;
  occurred_at?: string | null;
}

export type RetireGuidelineRequest =
  | (RetireGuidelineRequestBase & {
      status?: 'retired';
      superseded_by_guideline_id?: never;
    })
  | (RetireGuidelineRequestBase & {
      status: 'superseded';
      superseded_by_guideline_id: string;
    });

export interface RetirementResponse {
  retirement: GuidelineRetirement;
}

export interface PreviewGuidelineImpactRequest {
  target_revision_id: string;
  expected_binding_head_revision: number | null;
  enforcement: GuidelineEnforcement;
  minimum_confidence: number;
  metric_threshold_overrides: GuidelineMetricThresholdOverrides;
}

interface GuidelineImpactItemBase {
  impact_item_id: string;
  entity_id: string;
  details_digest: string;
  related_id: string | null;
  entity_version: number | null;
}

export interface GuidelineBindingImpactItem extends GuidelineImpactItemBase {
  item_kind: 'binding';
  entity_type: 'board';
}

export interface GuidelineSubjectImpactItem extends GuidelineImpactItemBase {
  item_kind: Exclude<GuidelineImpactItemKind, 'binding'>;
  entity_type: PolicyEntityType;
}

export type GuidelineImpactItem =
  | GuidelineBindingImpactItem
  | GuidelineSubjectImpactItem;

type GuidelineImpactPageItemBase = Omit<
  GuidelineImpactItemBase,
  'related_id' | 'entity_version'
> & {
  related_id?: string;
  entity_version?: number;
};

export type GuidelineImpactPageItem =
  | (GuidelineImpactPageItemBase & {
      item_kind: 'binding';
      entity_type: 'board';
    })
  | (GuidelineImpactPageItemBase & {
      item_kind: Exclude<GuidelineImpactItemKind, 'binding'>;
      entity_type: PolicyEntityType;
    });

export interface GuidelineImpactPreviewItemsPage {
  items: GuidelineImpactPageItem[];
  next_cursor: string | null;
}

export interface GuidelineImpactPreviewResponse {
  preview_id: string;
  preview_digest: string;
  items_page: GuidelineImpactPreviewItemsPage;
}

export interface AdoptGuidelineRevisionRequest {
  preview_id: string;
  preview_digest: string;
  expected_binding_head_revision: number | null;
  idempotency_key: string;
}

export interface GuidelineAdoptionResponse {
  binding_id: string;
  binding_revision: number;
  configuration_digest: string;
  replayed: boolean;
}

export type GuidelineHistoryStatus = 'complete' | 'baseline_only';
export type GuidelineImportTransactionStatus =
  | 'planned'
  | 'dry_run'
  | 'committed'
  | 'rolled_back';

export interface GuidelineExportIdentityV3 {
  guideline_id: string;
  owner_id: string;
  scope: PolicyGuidelineScope;
  board_id: string | null;
  context_scope: GuidelineContextScope;
  created_at: string;
}

export interface GuidelineExportHeadV3 {
  guideline_id: string;
  revision_id: string;
  revision_number: number;
  semantic_version: string;
  head_revision: number;
  updated_at: string;
}

export type GuidelineExportMetricV3 = GuidelineMetric;

export interface GuidelineExportRevisionV3 {
  revision_id: string;
  guideline_id: string;
  revision_number: number;
  semantic_version: string;
  title: string;
  content: string;
  revision_digest: string;
  metrics: GuidelineExportMetricV3[];
  created_by: string;
  created_at: string;
  parent_revision_id: string | null;
  tags: string[];
  published_head_revision: number;
  published_head_updated_at: string;
  legacy_version: string | null;
  legacy_version_unresolvable: boolean;
  legacy_tags: string[] | null;
}

export type GuidelineExportRetirementV3 = GuidelineRetirement;

export interface GuidelineExportLogicalBindingV3
  extends BoardGuidelineBinding {
  configuration_digest: string;
}

export interface GuidelineExportBindingV3 {
  binding: GuidelineExportLogicalBindingV3;
  physical_source_kind: string;
  binding_origin: string;
  materialization: GuidelineBindingMaterialization;
  legacy_source_id: string | null;
  legacy_guideline_version: string | null;
  legacy_template_id: string | null;
  legacy_template_version: string | null;
  legacy_version_unresolvable: boolean;
  evidence_refs: Array<[name: string, value: string]>;
  binding_digest: string;
}

export interface GuidelineExportAggregateV3 {
  identity: GuidelineExportIdentityV3;
  revisions: GuidelineExportRevisionV3[];
  head: GuidelineExportHeadV3;
  retirement: GuidelineExportRetirementV3 | null;
  bindings: GuidelineExportBindingV3[];
  history_status: GuidelineHistoryStatus;
  migration_notes: string[];
}

export interface GuidelineExportEnvelopeV3 {
  contract_version: 'guideline-export/v3';
  schema_version: '3';
  kind: 'guidelines';
  exported_at: string;
  source_board_id: string | null;
  content_digest: string;
  guidelines: GuidelineExportAggregateV3[];
}

export interface GuidelineImportResult {
  transaction_status: GuidelineImportTransactionStatus;
  created_count: number;
  skip_identical_count: number;
  conflict_count: number;
  overwritten_row_count: number;
  dry_run: boolean;
  error_code?: string | null;
}

export type PolicyErrorKind =
  | 'validation_failed'
  | 'under_bump'
  | 'permission_denied'
  | 'not_found'
  | 'conflict'
  | 'service_unavailable'
  | 'invalid_cursor';
export type PolicyErrorCategory =
  | 'invalid_argument'
  | 'permission_denied'
  | 'not_found'
  | 'conflict'
  | 'service_unavailable';

export interface PolicyErrorDetail {
  outcome: 'error';
  error: PolicyErrorKind;
  code: string;
  error_code: string;
  message: string;
  category: PolicyErrorCategory;
  status_category: string;
  http_status: 400 | 401 | 403 | 404 | 409 | 503;
  retryable: boolean;
  next_action: string;
  details: Record<string, string>;
}

export interface PolicyErrorEnvelope {
  detail: PolicyErrorDetail;
}

export interface PolicyClientErrorSnapshot {
  status: number;
  kind: PolicyErrorKind | null;
  code: string;
  category: PolicyErrorCategory | null;
  statusCategory: string | null;
  retryable: boolean;
  nextAction: string | null;
  details: Record<string, string>;
}

/**
 * UI state whose shape makes loading, transport failure, and denied authority
 * fail closed. A control can only be enabled for ready + allowed.
 */
export type PolicyActionState<T> =
  | {
      status: 'idle' | 'loading';
      data: null;
      error: null;
      authorization: 'unknown';
      controls_enabled: false;
    }
  | {
      status: 'error';
      data: null;
      error: PolicyClientErrorSnapshot;
      authorization: 'unknown';
      controls_enabled: false;
    }
  | {
      status: 'ready';
      data: T;
      error: null;
      authorization: 'denied';
      controls_enabled: false;
    }
  | {
      status: 'ready';
      data: T;
      error: null;
      authorization: 'allowed';
      controls_enabled: true;
    };

export function isPolicyActionEnabled<T>(
  state: PolicyActionState<T>,
): state is Extract<
  PolicyActionState<T>,
  { status: 'ready'; authorization: 'allowed' }
> {
  return (
    state.status === 'ready'
    && state.authorization === 'allowed'
    && state.controls_enabled
  );
}

export interface PolicyPageOptions {
  limit?: number;
  cursor?: string;
  projection?: PolicyProjection;
  signal?: AbortSignal;
}

export interface SemanticAssessmentPageOptions extends PolicyPageOptions {
  subjectType?: PolicyEntityType;
  subjectId?: string;
  guidelineId?: string;
  bindingId?: string;
  outcome?: SemanticAssessmentOutcome;
  currentness?: PolicyCurrentness;
}

export interface SemanticFindingPageOptions extends PolicyPageOptions {
  receiptId?: string;
  guidelineId?: string;
  bindingId?: string;
  metricId?: string;
  subjectType?: PolicyEntityType;
  subjectId?: string;
  outcome?: SemanticMetricOutcome;
}

export interface SemanticWaiverPageOptions extends PolicyPageOptions {
  evaluatedAt: string;
  findingId?: string;
  metricResultId?: string;
  receiptId?: string;
  guidelineId?: string;
  bindingId?: string;
  metricId?: string;
  subjectType?: PolicyEntityType;
  subjectId?: string;
  status?: PolicyWaiverStatus;
}

export interface SemanticSkipPageOptions extends PolicyPageOptions {
  subjectType?: PolicyEntityType;
  subjectId?: string;
  bindingId?: string;
  status?: SemanticSkipStatus;
  currentness?: PolicyCurrentness;
}

export interface RequestSemanticMetricWaiverRequest {
  metric_result_id: string;
  finding_id: string;
  receipt_id: string;
  justification: string;
  evidence_refs: NonEmptyArray<SemanticEvidenceRef>;
  expires_at: string | null;
  idempotency_key: string;
}

export interface RequestedSemanticWaiverResponse {
  waiver_id: string;
  status: 'requested';
  scope_digest: string;
}

export interface SemanticWaiverEvent {
  event_id: string;
  predecessor_event_id: string | null;
  waiver_id: string;
  waiver_revision: number;
  event_type: PolicyWaiverEventType;
  from_status: PolicyWaiverStatus | null;
  to_status: PolicyWaiverStatus;
  actor_id: string;
  occurred_at: string;
  reason: string;
  evidence_refs: SemanticEvidenceRef[];
  expires_at: string | null;
  scope_digest: string;
  waiver_digest: string;
  idempotency_key: string;
  request_digest: string;
  expire_reason: SemanticMetricWaiverExpireReason | null;
  evaluated_at: string | null;
  revalidation_status: SemanticMetricWaiverRevalidationStatus | null;
  revalidation_current: boolean | null;
  revalidation_reason_code:
    | SemanticMetricWaiverRevalidationReason
    | null;
  currentness_reasons: SemanticAssessmentCurrentnessReason[];
  scheduled_expiry_observed: boolean;
}

export interface SemanticWaiverEventsResponse {
  events: SemanticWaiverEvent[];
}

export interface ReviewSemanticMetricWaiverRequest {
  decision: PolicyWaiverReviewDecision;
  reason: string;
  evidence_refs: NonEmptyArray<SemanticEvidenceRef>;
  expected_waiver_revision: number;
  idempotency_key: string;
}

export interface RevokeSemanticMetricWaiverRequest {
  reason: string;
  evidence_refs: NonEmptyArray<SemanticEvidenceRef>;
  expected_waiver_revision: number;
  idempotency_key: string;
}

export interface RevalidateSemanticMetricWaiverRequest {
  expected_waiver_revision: number;
  evaluated_at: string;
  idempotency_key: string;
}

export interface ReviewedSemanticWaiverResponse {
  waiver_id: string;
  waiver_revision: number;
  status: 'approved' | 'rejected';
  reviewer_id: string;
  replayed: boolean;
}

export interface RevokedSemanticWaiverResponse {
  waiver_id: string;
  waiver_revision: number;
  status: 'revoked';
  replayed: boolean;
}

export interface RevalidatedSemanticWaiverResponse {
  waiver_id: string;
  waiver_revision: number;
  status: SemanticMetricWaiverRevalidationStatus;
  current: boolean;
  reason_code: SemanticMetricWaiverRevalidationReason;
  replayed: boolean;
}

export interface CreateSemanticSkipRequest {
  subject_type: PolicyEntityType;
  subject_id: string;
  expected_subject_version: number;
  binding_id: string;
  reason: string;
}

export interface CreatedSemanticSkipResponse {
  skip_id: string;
  scope_digest: string;
  created_by: string;
}

export interface RevokeSemanticSkipRequest {
  expected_skip_revision: number;
  reason: string;
  idempotency_key: string;
}

export interface RevokedSemanticSkipResponse {
  skip_id: string;
  skip_revision: number;
  status: 'revoked';
  revoked_by: string;
  replayed: boolean;
}

export interface GuidelineImpactItemPageOptions extends PolicyPageOptions {
  entityType?: PolicyEntityType;
  itemKind?: GuidelineImpactItemKind;
}
