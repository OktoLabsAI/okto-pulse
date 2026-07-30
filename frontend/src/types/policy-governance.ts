/**
 * Closed frontend contract for SK-B policy governance.
 *
 * These types intentionally do not extend the legacy Guideline or the SK-A
 * QualityAssessment contracts. Policy governance has its own versioned
 * identity, evidence, currentness, and waiver semantics.
 */

export type PolicyProjection = 'summary' | 'detail';

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
export type GuidelineRuleOperator = 'all' | 'any';
export type GuidelineBindingState = 'active' | 'unlinked';
export type GuidelineBindingProvenance =
  | 'native'
  | 'default_materialization';
export type GuidelineBindingMaterialization = 'live' | 'candidate';
export type GuidelineLifecycleStatus = 'retired' | 'superseded';
export type GuidelineRevisionMutationStatus = 'applied' | 'noop';
export type GuidelineVersionBump = 'patch' | 'minor' | 'major';
export type GuidelineImpactItemKind =
  | 'binding'
  | 'target'
  | 'artifact'
  | 'waiver';

export type PolicyEvaluationOutcome =
  | 'pass'
  | 'fail'
  | 'not_applicable'
  | 'error';
export type PolicyComplianceState =
  | 'ready'
  | 'blocked'
  | 'ready_with_waivers'
  | 'not_applicable';
export type PolicyCurrentness = 'current' | 'stale';
export type PolicyCurrentnessReason =
  | 'current_snapshot_missing'
  | 'subject_version_changed'
  | 'subject_content_changed'
  | 'input_digest_changed'
  | 'policy_set_changed'
  | 'catalog_version_changed'
  | 'ruleset_version_changed'
  | 'binding_head_changed';
export type PolicyComplianceReasonCode =
  | 'no_applicable_rules'
  | 'policy_evaluation_unavailable'
  | 'policy_evaluation_degraded';
export type PolicyTransitionReasonCode =
  | 'transition_not_allowed'
  | 'policy_compliance_not_required'
  | 'policy_compliance_receipt_missing'
  | 'policy_compliance_receipt_stale'
  | 'policy_compliance_blocked'
  | 'policy_evaluation_unavailable'
  | 'policy_evaluation_degraded'
  | 'policy_compliance_ready'
  | 'policy_compliance_ready_with_waivers'
  | 'policy_compliance_not_applicable'
  | 'policy_compliance_advisory_only'
  | 'policy_subject_required';

export interface PolicyComplianceTransitionDecision {
  state: PolicyTransitionReasonCode;
  allowed: boolean | null;
  policy_compliance_required: boolean;
  reason_codes: PolicyTransitionReasonCode[];
  decision_digest: string | null;
  fence_digest: string | null;
  receipt_id: string | null;
  currentness: PolicyCurrentness | null;
  currentness_reasons: PolicyCurrentnessReason[];
  applicable_rule_count: number | null;
  applicable_blocking_rule_count: number | null;
  blocking_rule_count: number | null;
  waived_rule_count: number | null;
  advisory_issue_count: number | null;
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
export type PolicyWaiverExpireReasonCode =
  | 'scheduled_expiry'
  | 'subject_scope_changed'
  | 'guideline_revision_changed'
  | 'guideline_rule_changed';

export type PolicyScalar = string | number | boolean | null;
export type PolicyParameterValue = PolicyScalar | PolicyScalar[];
export type NonEmptyArray<T> = [T, ...T[]];
export type AtLeastOne<T> = {
  [K in keyof T]-?: Required<Pick<T, K>> & Partial<Omit<T, K>>;
}[keyof T];
export type PolicyPredicateParameterEntries = Array<
  [name: string, value: PolicyParameterValue]
>;

export interface GuidelinePredicateInput {
  predicate_code: string;
  parameters?: Record<string, PolicyParameterValue>;
}

export interface GuidelinePredicate {
  predicate_code: string;
  parameters: PolicyPredicateParameterEntries;
}

export interface GuidelineRuleInput {
  rule_id: string;
  code: string;
  title: string;
  description: string;
  target_entity_types: NonEmptyArray<PolicyEntityType>;
  predicates: NonEmptyArray<GuidelinePredicateInput>;
  enforcement?: GuidelineEnforcement;
  operator?: GuidelineRuleOperator;
  waivable?: boolean;
  policy_class?: string;
}

export interface GuidelineRule {
  rule_id: string;
  code: string;
  title: string;
  description: string;
  target_entity_types: NonEmptyArray<PolicyEntityType>;
  predicates: NonEmptyArray<GuidelinePredicate>;
  enforcement: GuidelineEnforcement;
  operator: GuidelineRuleOperator;
  waivable: boolean;
  policy_class: string;
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
  content_digest: string;
  rules: GuidelineRule[];
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
  default_enforcement: GuidelineEnforcement;
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
  content_digest?: never;
  tags?: never;
  rules?: never;
}

export interface GuidelineRevisionDetail
  extends GuidelineRevisionListItemBase {
  projection: 'detail';
  content: string;
  content_digest: string;
  tags: string[];
  rules: GuidelineRule[];
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

interface GuidelineRevisionPatchFields {
  title: string;
  content: string;
  tags: string[];
  rules: GuidelineRuleInput[];
}

export type GuidelineRevisionPatchRequest =
  AtLeastOne<GuidelineRevisionPatchFields>;

export interface CreateGuidelineRevisionRequest {
  next_revision_id?: string | null;
  idempotency_key: string;
  patch: GuidelineRevisionPatchRequest;
  declared_semantic_version?: string | null;
  occurred_at?: string | null;
}

export type CreateGuidelineRevisionResponse =
  | {
      status: Extract<GuidelineRevisionMutationStatus, 'applied'>;
      revision: OmitNullAsOptional<
        GuidelineRevision,
        'parent_revision_id'
      >;
      head: GuidelineHead;
      minimum_bump: GuidelineVersionBump;
    }
  | {
      status: Extract<GuidelineRevisionMutationStatus, 'noop'>;
      revision?: never;
      head?: never;
      minimum_bump?: never;
    };

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
  proposed_priority: number;
  proposed_default_enforcement: GuidelineEnforcement;
  idempotency_key: string;
  to_revision_id?: string | null;
  requested_at?: string | null;
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

export interface GuidelineImpactReceipt {
  impact_receipt_id: string;
  board_id: string;
  guideline_id: string;
  binding_id: string;
  to_revision_id: string;
  to_revision_number: number;
  to_semantic_version: string;
  to_revision_digest: string;
  expected_head_revision: number;
  expected_binding_revision: number | null;
  expected_binding_state: GuidelineBindingState | null;
  binding_digest: string;
  binding_head_digest_before: string;
  binding_head_digest_after: string;
  policy_set_digest_before: string;
  policy_set_digest_after: string;
  artifact_snapshot_digest: string;
  waiver_snapshot_digest: string;
  proposed_priority: number;
  proposed_default_enforcement: GuidelineEnforcement;
  affected_entity_types: PolicyEntityType[];
  items: GuidelineImpactItem[];
  added_rule_ids: string[];
  changed_rule_ids: string[];
  removed_rule_ids: string[];
  requested_by: string;
  created_at: string;
  impact_digest: string;
  from_revision_id: string | null;
  from_semantic_version: string | null;
  from_revision_digest: string | null;
  requires_explicit_adoption: boolean;
}

export interface GuidelineImpactReceiptResponse {
  receipt: GuidelineImpactReceipt;
}

export interface AdoptGuidelineRevisionRequest {
  impact_receipt_id: string;
  impact_digest: string;
  idempotency_key: string;
  occurred_at?: string | null;
}

export interface GuidelineAdoptionResponse {
  binding: BoardGuidelineBinding;
  receipt: GuidelineImpactReceipt;
}

export interface PolicySubjectRef {
  board_id: string;
  entity_type: PolicyEntityType;
  subject_id: string;
  subject_version: number;
}

export interface AdoptedGuidelineRevisionRef {
  binding_id: string;
  binding_revision: number;
  guideline_id: string;
  revision_id: string;
  semantic_version: string;
  revision_digest: string;
}

export interface PolicyComplianceFinding {
  finding_id: string;
  receipt_id: string;
  subject: PolicySubjectRef;
  guideline_id: string;
  revision_id: string;
  rule_id: string;
  outcome: PolicyEvaluationOutcome;
  enforcement: GuidelineEnforcement;
  message: string;
  created_at: string;
  evidence_refs: string[];
  waiver_id: string | null;
}

export interface PolicyComplianceRuleResult {
  guideline_id: string;
  revision_id: string;
  rule_id: string;
  outcome: PolicyEvaluationOutcome;
  enforcement: GuidelineEnforcement;
  waiver_id: string | null;
}

export interface PolicyComplianceReceipt {
  receipt_id: string;
  subject: PolicySubjectRef;
  subject_content_digest: string;
  input_digest: string;
  policy_set_digest: string;
  binding_head_digest: string;
  catalog_version: string;
  ruleset_version: string;
  adopted_revisions: AdoptedGuidelineRevisionRef[];
  outcome: PolicyEvaluationOutcome;
  state: PolicyComplianceState;
  currentness: PolicyCurrentness;
  findings: PolicyComplianceFinding[];
  evaluator_version: string;
  evaluated_by: string;
  evaluated_at: string;
  rule_results: PolicyComplianceRuleResult[];
  reason_codes: PolicyComplianceReasonCode[];
}

export interface PolicyEvaluationResult {
  evaluation_id: string;
  input_digest: string;
  receipt: PolicyComplianceReceipt;
}

export interface EvaluatePolicyComplianceRequest {
  entity_type: PolicyEntityType;
  subject_id: string;
  idempotency_key: string;
  evaluation_id?: string | null;
  requested_at?: string | null;
  evaluated_at?: string | null;
}

export interface PolicyEvaluationResponse {
  evaluation: PolicyEvaluationResult;
}

export interface PolicyComplianceReceiptResponse {
  receipt: PolicyComplianceReceipt;
}

interface PolicyComplianceReceiptListItemBase {
  receipt_id: string;
  subject: PolicySubjectRef;
  outcome: PolicyEvaluationOutcome;
  state: PolicyComplianceState;
  currentness: PolicyCurrentness;
  currentness_reasons: PolicyCurrentnessReason[];
  evaluator_version: string;
  evaluated_by: string;
  evaluated_at: string;
  finding_count: number;
  rule_count: number;
  failed_rule_count: number;
  error_rule_count: number;
  blocking_finding_count: number;
  waived_finding_count: number;
  reason_codes: PolicyComplianceReasonCode[];
}

export interface PolicyComplianceReceiptSummary
  extends PolicyComplianceReceiptListItemBase {
  projection: 'summary';
  subject_content_digest?: never;
  input_digest?: never;
  policy_set_digest?: never;
  binding_head_digest?: never;
  catalog_version?: never;
  ruleset_version?: never;
  adopted_revisions?: never;
}

export interface PolicyComplianceReceiptDetail
  extends PolicyComplianceReceiptListItemBase {
  projection: 'detail';
  subject_content_digest: string;
  input_digest: string;
  policy_set_digest: string;
  binding_head_digest: string;
  catalog_version: string;
  ruleset_version: string;
  adopted_revisions: AdoptedGuidelineRevisionRef[];
}

export type PolicyComplianceReceiptListItem =
  | PolicyComplianceReceiptSummary
  | PolicyComplianceReceiptDetail;

interface PolicyComplianceFindingListItemBase {
  finding_id: string;
  receipt_id: string;
  subject: PolicySubjectRef;
  guideline_id: string;
  revision_id: string;
  rule_id: string;
  outcome: PolicyEvaluationOutcome;
  enforcement: GuidelineEnforcement;
  severity_rank: number;
  blocking: boolean;
  created_at: string;
  waiver_id?: string;
}

export interface PolicyComplianceFindingSummary
  extends PolicyComplianceFindingListItemBase {
  projection: 'summary';
  message?: never;
  evidence_refs?: never;
}

export interface PolicyComplianceFindingDetail
  extends PolicyComplianceFindingListItemBase {
  projection: 'detail';
  message: string;
  evidence_refs: string[];
}

export type PolicyComplianceFindingListItem =
  | PolicyComplianceFindingSummary
  | PolicyComplianceFindingDetail;

export interface PolicyWaiver {
  waiver_id: string;
  board_id: string;
  finding_id: string;
  receipt_id: string;
  guideline_id: string;
  revision_id: string;
  rule_id: string;
  subject: PolicySubjectRef;
  status: PolicyWaiverStatus;
  justification: string;
  evidence_refs: string[];
  requested_by: string;
  requested_at: string;
  waiver_revision: number;
  expires_at: string;
  last_event_id: string;
  last_event_type: PolicyWaiverEventType;
  last_event_at: string;
  reviewed_by: string | null;
  reviewed_at: string | null;
  review_reason: string | null;
  revoked_by: string | null;
  revoked_at: string | null;
  expire_reason_code: PolicyWaiverExpireReasonCode | null;
}

export interface PolicyWaiverEvent {
  event_id: string;
  waiver_id: string;
  board_id: string;
  waiver_revision: number;
  event_type: PolicyWaiverEventType;
  from_status: PolicyWaiverStatus | null;
  to_status: PolicyWaiverStatus;
  actor_id: string;
  occurred_at: string;
  reason: string;
  evidence_refs: string[];
  expires_at: string;
  scope_digest: string;
  expire_reason_code: PolicyWaiverExpireReasonCode | null;
}

interface PolicyWaiverListItemBase {
  waiver_id: string;
  board_id: string;
  finding_id: string;
  receipt_id: string;
  guideline_id: string;
  revision_id: string;
  rule_id: string;
  subject: PolicySubjectRef;
  status: PolicyWaiverStatus;
  source_current: boolean;
  effective: boolean;
  requested_by: string;
  requested_at: string;
  expires_at: string;
  waiver_revision: number;
  last_event_at: string;
  expire_reason_code?: PolicyWaiverExpireReasonCode;
}

export interface PolicyWaiverSummary extends PolicyWaiverListItemBase {
  projection: 'summary';
  justification?: never;
  evidence_refs?: never;
  reviewed_by?: never;
  reviewed_at?: never;
  review_reason?: never;
  revoked_by?: never;
  revoked_at?: never;
}

export interface PolicyWaiverDetail extends PolicyWaiverListItemBase {
  projection: 'detail';
  justification: string;
  evidence_refs: string[];
  reviewed_by?: string;
  reviewed_at?: string;
  review_reason?: string;
  revoked_by?: string;
  revoked_at?: string;
}

export type PolicyWaiverListItem = PolicyWaiverSummary | PolicyWaiverDetail;

export interface RequestPolicyWaiverRequest {
  waiver_id?: string | null;
  event_id?: string | null;
  finding_id: string;
  justification: string;
  evidence_refs: NonEmptyArray<string>;
  expires_at: string;
  idempotency_key: string;
  occurred_at?: string | null;
}

export interface ReviewPolicyWaiverRequest {
  decision: PolicyWaiverReviewDecision;
  reason: string;
  evidence_refs: NonEmptyArray<string>;
  expected_waiver_revision: number;
  idempotency_key: string;
  event_id?: string | null;
  occurred_at?: string | null;
}

export interface RevokePolicyWaiverRequest {
  reason: string;
  evidence_refs: NonEmptyArray<string>;
  expected_waiver_revision: number;
  idempotency_key: string;
  event_id?: string | null;
  occurred_at?: string | null;
}

export interface RevalidatePolicyWaiverRequest
  extends RevokePolicyWaiverRequest {
  new_expires_at: string;
}

export interface PolicyWaiverResponse {
  waiver: PolicyWaiver;
}

export interface PolicyWaiverEventsResponse {
  events: PolicyWaiverEvent[];
}

export interface PolicyWaiverMutationResponse {
  waiver: PolicyWaiver;
  event: PolicyWaiverEvent;
}

export type GuidelineHistoryStatus = 'complete' | 'baseline_only';
export type GuidelineImportTransactionStatus =
  | 'planned'
  | 'dry_run'
  | 'committed'
  | 'rolled_back';

export interface GuidelineExportIdentityV2 {
  guideline_id: string;
  owner_id: string;
  scope: PolicyGuidelineScope;
  board_id: string | null;
  context_scope: GuidelineContextScope;
  created_at: string;
}

export interface GuidelineExportPredicateV2 {
  predicate_code: string;
  parameters: PolicyPredicateParameterEntries;
}

export interface GuidelineExportRuleV2
  extends Omit<GuidelineRule, 'predicates' | 'policy_class'> {
  predicates: GuidelineExportPredicateV2[];
  policy_class: string | null;
}

export interface GuidelineExportRevisionV2 {
  revision_id: string;
  guideline_id: string;
  revision_number: number;
  semantic_version: string;
  title: string;
  content: string;
  content_digest: string;
  rules: GuidelineExportRuleV2[];
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

export interface GuidelineExportHeadV2 {
  guideline_id: string;
  revision_id: string;
  revision_number: number;
  semantic_version: string;
  head_revision: number;
  updated_at: string;
}

export type GuidelineExportRetirementV2 = GuidelineRetirement;

export type GuidelineExportLogicalBindingV2 = BoardGuidelineBinding;

export interface GuidelineExportBindingV2 {
  binding: GuidelineExportLogicalBindingV2;
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

export interface GuidelineExportAggregateV2 {
  identity: GuidelineExportIdentityV2;
  revisions: GuidelineExportRevisionV2[];
  head: GuidelineExportHeadV2;
  retirement: GuidelineExportRetirementV2 | null;
  bindings: GuidelineExportBindingV2[];
  history_status: GuidelineHistoryStatus;
  migration_notes: string[];
}

export interface GuidelineExportEnvelopeV2 {
  contract_version: 'guideline-export/v2';
  schema_version: '2';
  kind: 'guidelines';
  exported_at: string;
  source_board_id: string | null;
  content_digest: string;
  guidelines: GuidelineExportAggregateV2[];
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

export interface GuidelineImpactItemPageOptions extends PolicyPageOptions {
  entityType?: PolicyEntityType;
  itemKind?: GuidelineImpactItemKind;
}

export interface PolicyComplianceReceiptPageOptions extends PolicyPageOptions {
  entityType?: PolicyEntityType;
  subjectId?: string;
  outcome?: PolicyEvaluationOutcome;
  currentness?: PolicyCurrentness;
}

export interface PolicyComplianceFindingPageOptions extends PolicyPageOptions {
  receiptId?: string;
  guidelineId?: string;
  ruleId?: string;
  subjectId?: string;
  outcome?: PolicyEvaluationOutcome;
}

export interface PolicyWaiverPageOptions extends PolicyPageOptions {
  evaluatedAt: string;
  findingId?: string;
  receiptId?: string;
  guidelineId?: string;
  revisionId?: string;
  ruleId?: string;
  entityType?: PolicyEntityType;
  subjectId?: string;
  subjectVersion?: number;
  status?: PolicyWaiverStatus;
}
