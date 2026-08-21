/**
 * Type definitions for the Dashboard application
 */

import type {
  PolicyComplianceTransitionDecision,
  RedactedPolicyComplianceTransitionDecision,
} from './policy-governance';

// Card status enum matching backend
export type CardStatus =
  | 'not_started'
  | 'started'
  | 'in_progress'
  | 'validation'
  | 'rejected'
  | 'on_hold'
  | 'done'
  | 'cancelled';

export const CARD_STATUSES: CardStatus[] = [
  'not_started',
  'started',
  'in_progress',
  'validation',
  'rejected',
  'on_hold',
  'done',
  'cancelled',
];

/**
 * Card creation may only enter at the beginning of the lifecycle. Rejected
 * and every advanced state are consequences of governed transitions.
 */
export const CREATABLE_CARD_STATUSES: CardStatus[] = [
  'not_started',
  'started',
];

export const STATUS_LABELS: Record<CardStatus, string> = {
  not_started: 'Not Started',
  started: 'Started',
  in_progress: 'In Progress',
  validation: 'Validation',
  rejected: 'Rejected',
  on_hold: 'On Hold',
  done: 'Done',
  cancelled: 'Cancelled',
};

// Card priority
export type CardPriority = 'critical' | 'very_high' | 'high' | 'medium' | 'low' | 'none';

export const CARD_PRIORITIES: CardPriority[] = [
  'critical', 'very_high', 'high', 'medium', 'low', 'none',
];

export const PRIORITY_LABELS: Record<CardPriority, string> = {
  critical: 'Critical',
  very_high: 'Very High',
  high: 'High',
  medium: 'Medium',
  low: 'Low',
  none: 'None',
};

export const PRIORITY_COLORS: Record<CardPriority, { badge: string; borderColor: string; dark_badge: string }> = {
  critical: { badge: 'bg-red-100 text-red-700', dark_badge: 'dark:bg-red-900/40 dark:text-red-300', borderColor: '#ef4444' },
  very_high: { badge: 'bg-orange-100 text-orange-700', dark_badge: 'dark:bg-orange-900/40 dark:text-orange-300', borderColor: '#f97316' },
  high: { badge: 'bg-amber-100 text-amber-700', dark_badge: 'dark:bg-amber-900/40 dark:text-amber-300', borderColor: '#f59e0b' },
  medium: { badge: 'bg-yellow-100 text-yellow-700', dark_badge: 'dark:bg-yellow-900/40 dark:text-yellow-300', borderColor: '#facc15' },
  low: { badge: 'bg-blue-100 text-blue-700', dark_badge: 'dark:bg-blue-900/40 dark:text-blue-300', borderColor: '#60a5fa' },
  none: { badge: 'bg-gray-100 text-gray-500', dark_badge: 'dark:bg-gray-800 dark:text-gray-400', borderColor: '' },
};

// Card type
export type CardType = 'normal' | 'bug' | 'test';

export type LineageEntityType =
  | 'story'
  | 'ideation'
  | 'refinement'
  | 'spec'
  | 'sprint'
  | 'task'
  | 'test'
  | 'bug'
  | 'card'
  | 'artifact';

export interface LineageGraphNode {
  id: string;
  entity_type: LineageEntityType;
  entity_id: string;
  title: string;
  label: string;
  status?: string | null;
  stage: number;
  card_type?: CardType | string;
  artifact_type?: string;
  source_entity_type?: string;
  source_entity_id?: string;
  summary?: Record<string, unknown>;
  resource_counts?: LineageResourceCounts;
}

export interface LineageResourceCounts {
  unique_effective_count: number;
  raw_attachment_count: number;
  workspace_item_count: number;
  /** Distinct root/version projections; absent on rolling-upgrade servers. */
  unique_root_version_count?: number;
}

export interface LineageGraphEdge {
  id: string;
  source: string;
  target: string;
  relationship: string;
}

export interface LineageGraphResponse {
  board_id: string;
  selected: {
    entity_type: string;
    entity_id: string;
  };
  root_ideation: {
    id: string;
    title: string;
    status?: string | null;
  };
  resolution_path: Array<{ type: string; id: string }>;
  nodes: LineageGraphNode[];
  edges: LineageGraphEdge[];
  summary: Record<string, number>;
  resource_counts?: LineageResourceCounts;
  warnings: string[];
}

export type AllowedTransitionEntityType =
  | 'story'
  | 'ideation'
  | 'refinement'
  | 'spec'
  | 'card'
  | 'sprint'
  | 'test_scenario';

export interface AllowedTransition {
  to_status: string;
  label: string;
  gate: string;
  blocked_reason: string | null;
  blocked_facts: Record<string, unknown> | null;
  preconditions: string[];
  capabilities: string[];
  effects: string[];
  reason_codes: string[];
  policy_compliance: boolean;
  policy_compliance_decision:
    | PolicyComplianceTransitionDecision
    | RedactedPolicyComplianceTransitionDecision
    | null;
}

export interface AllowedTransitionsResponse {
  board_id: string;
  entity_type: AllowedTransitionEntityType;
  entity_id: string | null;
  current_status: string;
  allowed_transitions: AllowedTransition[];
  source: string;
}

export type ResourceGateEntityType = 'ideation' | 'refinement' | 'spec' | 'card';
export type ResourceGateResourceType = 'architecture' | 'mockup' | 'knowledge_base';
export type ResourceGateState = 'provided' | 'not_applicable' | 'missing';

export interface ResourceGateRef {
  id: string;
  title?: string | null;
  source_entity_type?: string | null;
  source_entity_id?: string | null;
  source_entity_title?: string | null;
  [key: string]: unknown;
}

export interface ResourceGateNaMark {
  id?: string;
  active: boolean;
  effective?: boolean;
  justification?: string | null;
  source_channel?: 'ui' | 'api' | 'mcp' | string;
  created_by?: string | null;
  created_at?: string | null;
}

export interface ResourceGateResource {
  resource_type: ResourceGateResourceType;
  state: ResourceGateState;
  authority?: 'blocking' | 'advisory';
  blocking?: boolean;
  direct_count: number;
  inherited_count: number;
  direct_refs?: ResourceGateRef[];
  inherited_refs?: ResourceGateRef[];
  na_mark?: ResourceGateNaMark | null;
  remediation?: string | null;
  reason?: string | null;
}

export interface ResourceGateSummary {
  board_id: string;
  entity_type: ResourceGateEntityType;
  entity_id: string;
  resources: ResourceGateResource[];
  blocking: boolean;
  missing_resources: ResourceGateResource[];
  advisory_resources?: ResourceGateResource[];
  advisory_missing_resources?: ResourceGateResource[];
  authority_policy?: {
    policy_version?: number;
    context?: string;
    blocking_resource_types?: ResourceGateResourceType[];
    advisory_resource_types?: ResourceGateResourceType[];
  };
  warnings: Array<{ code?: string; message: string; resource_type?: string }>;
}

export interface EffectiveResourceItem extends ResourceGateRef {
  resource_type: ResourceGateResourceType;
  resource_id?: string | null;
  attachment_kind: 'direct' | 'inherited_reference' | string;
  inherited: boolean;
  read_only: boolean;
  hydrated: boolean;
  hydration_error?: string | null;
  provenance?: {
    source_entity_type?: string | null;
    source_entity_id?: string | null;
    source_entity_title?: string | null;
    resource_id?: string | null;
  };
  ref?: {
    root_resource_id?: string | null;
    knowledge_assignment_id?: string | null;
    knowledge_assignment_mode?: KnowledgePropagationMode | null;
    knowledge_assignment_state?: KnowledgeAssignmentState | null;
    knowledge_assignment_stale?: boolean | null;
    origin_class?: KnowledgeOriginClass | null;
    [key: string]: unknown;
  };
  resource?: Record<string, unknown> | ArchitectureDesign | ScreenMockup | null;
}

export type KnowledgeWorkspaceProfile = 'summary' | 'detail' | 'full' | 'legacy';

export interface KnowledgeWorkspacePhysicalAttachment {
  resource_id: string | null;
  attachment_kind: string | null;
  inherited: boolean;
  source_entity_type: string | null;
  source_entity_id: string | null;
  source_entity_title: string | null;
  effective: boolean;
  resource_version: string | null;
  revision_stamp: Record<string, unknown> | null;
}

export interface KnowledgeWorkspaceItem {
  resource_type: ResourceGateResourceType;
  canonical_unique_resource_id: string;
  versioned_projection_id: string;
  root_id: string;
  resource_version: string | null;
  representative_resource_id: string | null;
  title: string | null;
  attachment_kind: string | null;
  inherited: boolean;
  grandfathered: boolean;
  stale: boolean;
  superseded: boolean;
  provenance: {
    source_entity_type: string | null;
    source_entity_id: string | null;
    source_entity_title: string | null;
    origin_class: string | null;
    source_revision: string | null;
    source_content_sha256: string | null;
  };
  physical_attachments: KnowledgeWorkspacePhysicalAttachment[];
  detail_cursor: string;
  relevance_links: Array<Record<string, unknown>>;
  body?: unknown;
  body_omitted_reason?: 'profile_summary' | 'body_unavailable' | 'body_size_limit' | 'response_budget' | string;
  body_ref?: {
    resource_type: ResourceGateResourceType;
    resource_id: string | null;
  };
}

export interface EffectiveResourcesOptions {
  profile?: KnowledgeWorkspaceProfile;
  cursor?: string | null;
  limit?: number;
}

export interface EffectiveResourcesResponse {
  contract_version?: number;
  board_id: string;
  entity_type: ResourceGateEntityType;
  entity_id: string;
  profile?: KnowledgeWorkspaceProfile;
  items?: KnowledgeWorkspaceItem[];
  count?: number;
  total_count?: number;
  next_cursor?: string | null;
  truncated?: boolean;
  unique_effective_count?: number;
  raw_attachment_count?: number;
  workspace_item_count?: number;
  unique_root_version_count?: number;
  response_bytes?: number;
  /**
   * Populated by the explicit `legacy` profile. Kept mandatory in the
   * normalized client result so rolling upgrades do not break older callers.
   */
  resources: Record<ResourceGateResourceType, EffectiveResourceItem[]>;
  lineage_counts?: Record<string, unknown>;
  resource_lineage?: Record<string, unknown>;
}

export interface MarkResourceNotApplicableRequest {
  resource_type: ResourceGateResourceType;
  source_channel?: 'ui' | 'api' | 'mcp';
  justification?: string | null;
}

export interface ClearResourceNotApplicableRequest {
  source_channel?: 'ui' | 'api' | 'mcp';
  reason?: string | null;
}

// Bug severity
export type BugSeverity = 'critical' | 'major' | 'minor';

export const BUG_SEVERITY_LABELS: Record<BugSeverity, string> = {
  critical: 'Critical',
  major: 'Major',
  minor: 'Minor',
};

export const BUG_SEVERITY_COLORS: Record<BugSeverity, { badge: string; dark_badge: string }> = {
  critical: { badge: 'bg-red-100 text-red-700', dark_badge: 'dark:bg-red-900/40 dark:text-red-300' },
  major: { badge: 'bg-orange-100 text-orange-700', dark_badge: 'dark:bg-orange-900/40 dark:text-orange-300' },
  minor: { badge: 'bg-yellow-100 text-yellow-700', dark_badge: 'dark:bg-yellow-900/40 dark:text-yellow-300' },
};

// Attachment
export interface Attachment {
  id: string;
  card_id: string;
  filename: string;
  original_filename: string;
  mime_type: string;
  size: number;
  uploaded_by: string;
  created_at: string;
}

// Q&A Item
export interface QAItem {
  id: string;
  card_id: string;
  question: string;
  answer: string | null;
  asked_by: string;
  answered_by: string | null;
  created_at: string;
  answered_at: string | null;
}

// Choice board types
export interface ChoiceOption {
  id: string;
  label: string;
}

export interface ChoiceResponse {
  responder_id: string;
  responder_name: string;
  selected: string[];
  free_text?: string;
}

// Comment
export interface Comment {
  id: string;
  card_id: string;
  content: string;
  author_id: string;
  comment_type?: 'text' | 'choice' | 'multi_choice';
  choices?: ChoiceOption[];
  responses?: ChoiceResponse[];
  allow_free_text?: boolean;
  created_at: string;
  updated_at: string;
}

// Spec status
export type SpecStatus = 'draft' | 'review' | 'approved' | 'validated' | 'in_progress' | 'done' | 'cancelled';

export const SPEC_STATUSES: SpecStatus[] = [
  'draft', 'review', 'approved', 'validated', 'in_progress', 'done', 'cancelled',
];

export const SPEC_STATUS_LABELS: Record<SpecStatus, string> = {
  draft: 'Draft',
  review: 'Review',
  approved: 'Approved',
  validated: 'Validated',
  in_progress: 'In Progress',
  done: 'Done',
  cancelled: 'Cancelled',
};

// Card summary for spec context
export interface CardSummaryForSpec {
  id: string;
  title: string;
  status: CardStatus;
  priority: CardPriority;
  assignee_id: string | null;
  card_type?: CardType;
  sprint_id?: string | null;
}

// Sprint Status
export type SprintStatus = 'draft' | 'active' | 'review' | 'closed' | 'cancelled';
export type SprintLaneType = 'normal' | 'hotfix';

export const SPRINT_STATUSES: SprintStatus[] = [
  'draft', 'active', 'review', 'closed', 'cancelled',
];

export const SPRINT_STATUS_LABELS: Record<SprintStatus, string> = {
  draft: 'Draft',
  active: 'Active',
  review: 'Review',
  closed: 'Closed',
  cancelled: 'Cancelled',
};

export const SPRINT_STATUS_COLORS: Record<SprintStatus, string> = {
  draft: 'bg-gray-500',
  active: 'bg-blue-500',
  review: 'bg-amber-500',
  closed: 'bg-green-500',
  cancelled: 'bg-red-500',
};

export interface TaskValidationGateOverride {
  require_task_validation?: boolean | null;
  validation_min_confidence?: number | null;
  validation_min_completeness?: number | null;
  validation_max_drift?: number | null;
}

export interface Sprint extends TaskValidationGateOverride {
  id: string;
  spec_id: string;
  board_id: string;
  title: string;
  description: string | null;
  objective: string | null;
  expected_outcome: string | null;
  status: SprintStatus;
  lane_type: SprintLaneType;
  origin_sprint_id: string | null;
  origin_bug_id: string | null;
  normal_sprint_created: boolean;
  spec_version: number;
  start_date: string | null;
  end_date: string | null;
  test_scenario_ids: string[] | null;
  business_rule_ids: string[] | null;
  evaluations: any[] | null;
  skip_test_coverage: boolean;
  skip_rules_coverage: boolean;
  skip_qualitative_validation: boolean;
  validation_threshold: number | null;
  version: number;
  labels: string[] | null;
  archived: boolean;
  // Cancellation justification (set only while status === 'cancelled')
  cancellation_reason?: string | null;
  cancelled_at?: string | null;
  cancelled_by?: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
  cards: CardSummaryForSpec[];
  qa_items: SprintQAItem[];
}

export interface SprintSummary {
  id: string;
  open_qa_count?: number;
  spec_id: string;
  board_id: string;
  title: string;
  description: string | null;
  objective?: string | null;
  expected_outcome?: string | null;
  status: SprintStatus;
  lane_type: SprintLaneType;
  origin_sprint_id: string | null;
  origin_bug_id: string | null;
  normal_sprint_created: boolean;
  spec_version: number;
  start_date: string | null;
  end_date: string | null;
  test_scenario_ids: string[] | null;
  business_rule_ids: string[] | null;
  version: number;
  labels: string[] | null;
  created_by: string;
  created_at: string;
  updated_at: string;
  archived: boolean;
  cancellation_reason?: string | null;
  cancelled_at?: string | null;
  cancelled_by?: string | null;
}

export interface SprintQAItem {
  id: string;
  sprint_id: string;
  question: string;
  question_type: string;
  choices: any[] | null;
  allow_free_text: boolean;
  answer: string | null;
  selected: string[] | null;
  asked_by: string;
  answered_by: string | null;
  created_at: string;
  answered_at: string | null;
}

export interface CreateSprintRequest {
  title: string;
  description?: string;
  objective?: string;
  expected_outcome?: string;
  spec_id: string;
  lane_type?: SprintLaneType;
  origin_sprint_id?: string | null;
  origin_bug_id?: string | null;
  test_scenario_ids?: string[];
  business_rule_ids?: string[];
  start_date?: string;
  end_date?: string;
  labels?: string[];
}

export interface UpdateSprintRequest extends TaskValidationGateOverride {
  title?: string;
  description?: string | null;
  objective?: string | null;
  expected_outcome?: string | null;
  lane_type?: SprintLaneType;
  origin_sprint_id?: string | null;
  origin_bug_id?: string | null;
  test_scenario_ids?: string[];
  business_rule_ids?: string[];
  start_date?: string | null;
  end_date?: string | null;
  labels?: string[];
  skip_test_coverage?: boolean;
  skip_rules_coverage?: boolean;
  skip_qualitative_validation?: boolean;
  validation_threshold?: number | null;
  expected_version?: number;
}

export interface MoveSprintRequest {
  status: SprintStatus;
  cancellation_reason?: string;
  expected_version?: number;
}

// Ideation Status
export type IdeationStatus = 'draft' | 'review' | 'approved' | 'evaluating' | 'done' | 'cancelled';
export type IdeationComplexity = 'small' | 'medium' | 'large';

export const IDEATION_STATUSES: IdeationStatus[] = ['draft', 'review', 'approved', 'evaluating', 'done', 'cancelled'];

export const IDEATION_STATUS_LABELS: Record<IdeationStatus, string> = {
  draft: 'Draft',
  review: 'Review',
  approved: 'Approved',
  evaluating: 'Evaluating',
  done: 'Done',
  cancelled: 'Cancelled',
};

export const COMPLEXITY_LABELS: Record<IdeationComplexity, string> = {
  small: 'Small',
  medium: 'Medium',
  large: 'Large',
};

// Refinement Status
export type RefinementStatus = 'draft' | 'review' | 'approved' | 'done' | 'cancelled';

export const REFINEMENT_STATUSES: RefinementStatus[] = ['draft', 'review', 'approved', 'done', 'cancelled'];

export const REFINEMENT_STATUS_LABELS: Record<RefinementStatus, string> = {
  draft: 'Draft',
  review: 'Review',
  approved: 'Approved',
  done: 'Done',
  cancelled: 'Cancelled',
};

// Governed quality assessments (SK-A).
//
// The API deliberately exposes three related but distinct projections:
// list summaries use scale.min/max, receipts use scale.minimum/maximum, and
// list-item currentness is an object rather than the current endpoint string.
// Keep those shapes separate so a backend contract drift fails at compile time
// instead of being silently normalized by the UI.
export type QualitySubjectType = 'ideation' | 'refinement' | 'spec';
export type QualityAssessmentKind =
  | 'ambiguity'
  | 'spec_validation'
  | 'requirement_lint';
export type QualityCurrentness = 'current' | 'stale';
/** Human lifecycle projection; does not expose technical staleness semantics. */
export type QualityLifecycleState = 'current' | 'previous';
export type QualityAssessmentStaleReason =
  | 'content_changed'
  | 'clarification_changed'
  | 'ruleset_changed'
  | 'taxonomy_changed'
  | 'policy_changed'
  | 'subject_version_changed';
export type QualityAssessmentReceiptState =
  | 'current'
  | 'previous'
  | 'history_only'
  // Legacy technical values remain parseable outside lifecycle-edition mode.
  | 'stale'
  | 'superseded';
export type QualityFindingSeverity =
  | 'info'
  | 'low'
  | 'medium'
  | 'high'
  | 'critical';
export type QualityFindingLifecycle = 'open' | 'resolved' | 'superseded';
export type QualityFindingAnchorType =
  | 'whole_artifact'
  | 'field'
  | 'structured_child'
  | 'qa';
export type QualityScaleKind =
  | 'ambiguity_score'
  | 'percentage'
  | 'finding_count';
export type QualityScaleDirection = 'lower_better' | 'higher_better';

export interface QualityScaleSummary {
  kind: QualityScaleKind;
  min: number;
  max: number;
  direction: QualityScaleDirection;
}

export interface QualityAssessmentSummary {
  /** Human validation edition. Null means legacy, history-only evidence. */
  edition?: number | null;
  /** Human projection for the live edition. */
  state?: 'current' | 'previous' | 'not_started';
  previous_count?: number;
  /** Null when this edition has not been assessed. */
  current_result?: {
    score: number;
    scale: QualityScaleSummary;
  } | null;
}

/**
 * Optional on list entities by design:
 * - omitted: the actor cannot read Quality (or the projection was not asked);
 * - {}: the actor may read Quality, but the projection returned no assessment
 *   kinds. Returned lifecycle kinds represent an empty current slot explicitly
 *   with `state: 'not_started'` and `current_result: null`.
 */
export type QualitySummaryMap = Partial<
  Record<QualityAssessmentKind, QualityAssessmentSummary>
>;

export interface QualityReceiptScale {
  kind: QualityScaleKind;
  minimum: number;
  maximum: number;
  direction: QualityScaleDirection;
}

export interface QualityReceiptDigests {
  content_digest: string;
  clarification_digest: string;
  ruleset_digest: string;
  taxonomy_digest: string;
  policy_digest: string;
  input_digest: string;
  canonicalization_version: string;
}

export interface QualityReceiptVersions {
  ruleset_version: string;
  taxonomy_version: string;
  analyzer_version: string;
  policy_version: string;
}

export interface QualityAssessmentReceipt {
  id: string;
  board_id: string;
  subject_type: QualitySubjectType;
  subject_id: string;
  subject_version: number;
  /** Null is reserved for evidence created before lifecycle editions. */
  subject_edition?: number | null;
  assessment_kind: QualityAssessmentKind;
  origin:
    | 'human_or_agent'
    | 'spec_validation'
    | 'semantic_writer'
    | 'legacy_import';
  source: 'native' | 'legacy_migration';
  channel: string;
  outcome: 'recorded' | 'advisory';
  scale: QualityReceiptScale;
  score: number;
  justification: string;
  digests: QualityReceiptDigests;
  versions: QualityReceiptVersions;
  run_identity_digest: string;
  authority_digest: string;
  idempotency_key: string;
  request_digest: string;
  created_by: string;
  created_at: string;
  predecessor_receipt_id: string | null;
  contract_version: 'quality-assessment/v1';
}

export interface QualityReceiptCurrentness {
  current: boolean;
  state: QualityLifecycleState;
  stale_reasons: QualityAssessmentStaleReason[];
}

export interface QualityAssessmentListItem {
  receipt: QualityAssessmentReceipt;
  is_head: boolean;
  state: QualityAssessmentReceiptState;
  currentness: QualityReceiptCurrentness;
}

export type QualityGateReasonCode =
  | 'not_applicable'
  | 'ambiguity_gate_disabled'
  | 'ambiguity_gate_skipped'
  | 'ambiguity_assessment_stale'
  | 'ambiguity_score_exceeds_threshold'
  | 'ambiguity_gate_ready';

export interface QualityGatePreview {
  applicable: boolean;
  enabled: boolean;
  allowed: boolean;
  reason_code: QualityGateReasonCode;
  threshold: number | null;
  score: number;
  skipped: boolean;
}

export interface CurrentQualityAssessment {
  receipt: QualityAssessmentReceipt;
  edition?: number | null;
  lifecycle_state?: QualityLifecycleState;
  head_revision: number;
  currentness: QualityLifecycleState;
  stale_reasons: QualityAssessmentStaleReason[];
  gate_preview: QualityGatePreview;
}

export interface QualityAssessmentReceiptDetail {
  receipt: QualityAssessmentReceipt;
  currentness: QualityLifecycleState;
  stale_reasons: QualityAssessmentStaleReason[];
}

export interface QualityEvidenceRef {
  source_type: string;
  source_id: string;
  source_version: number;
  content_hash: string;
}

export interface QualityFindingAnchor {
  board_id: string;
  subject_type: QualitySubjectType;
  subject_id: string;
  subject_version: number;
  input_digest: string;
  anchor_type: QualityFindingAnchorType;
  anchor_ref: string | null;
  excerpt_hash: string | null;
}

export interface QualityFinding {
  id: string;
  receipt_id: string;
  assessment_kind: QualityAssessmentKind;
  finding_key: string;
  category_code: string;
  taxonomy_version: string;
  severity: QualityFindingSeverity;
  confidence: number;
  deterministic: boolean;
  blocking_eligible: boolean;
  title: string;
  detail: string;
  anchor: QualityFindingAnchor;
  evidence_refs: QualityEvidenceRef[];
  lifecycle: QualityFindingLifecycle;
  created_at: string;
  remediation: string | null;
  rule_code: string | null;
}

export interface QualityFindingInput {
  finding_key: string;
  category_code: string;
  severity: QualityFindingSeverity;
  confidence: number;
  deterministic: boolean;
  title: string;
  detail: string;
  anchor: {
    anchor_type: QualityFindingAnchorType;
    anchor_ref?: string | null;
    excerpt_hash?: string | null;
  };
  evidence_refs: QualityEvidenceRef[];
  remediation?: string | null;
  rule_code?: string | null;
}

export interface QualityProposedQuestionInput {
  client_key: string;
  question: string;
  question_type: string;
  choices: string[];
  allow_free_text: boolean;
  category_code?: string | null;
  finding_keys: string[];
}

export interface RecordAmbiguityAssessmentRequest {
  idempotency_key: string;
  expected_subject_version: number;
  expected_subject_edition: number;
  expected_head_revision: number;
  score: number;
  findings: QualityFindingInput[];
  proposed_questions: QualityProposedQuestionInput[];
}

export interface RecordAmbiguityAssessmentResponse {
  outcome: 'success';
  replayed: boolean;
  receipt_id: string;
  head_revision: number;
  subject_edition?: number | null;
  qa_id_map: Record<string, string>;
}

export type ValidationCycleState =
  | 'not_started'
  | 'pending'
  | 'in_progress'
  | 'completed';

export type ValidationCycleResultType =
  | 'ambiguity_assessment'
  | 'spec_validation'
  | 'requirement_lint'
  | 'curated_checklist'
  | 'policy_compliance';

export interface ValidationSubmissionFence {
  expected_validation_edition: number;
  expected_subject_version: number;
  expected_head_revision: number;
}

export interface ValidationCycleResultSummary {
  result_id: string;
  result_type: ValidationCycleResultType;
  /** Null identifies evidence created before lifecycle editions. */
  subject_edition: number | null;
  status: string;
  summary: Record<string, unknown>;
}

export type PolicyComplianceLifecycleBindingStatus =
  | 'passed'
  | 'failed'
  | 'waived'
  | 'skipped'
  | 'pending'
  | 'inconsistent';

export type PolicyComplianceLifecycleMetricOutcome =
  | 'passed'
  | 'failed'
  | 'waived'
  | 'pending';

export interface PolicyComplianceLifecycleCounts {
  applicable: number;
  completed: number;
  passed: number;
  failed: number;
  waived: number;
  skipped: number;
  pending: number;
  context_only: number;
  inconsistent: number;
  /** Frozen scope items whose applicability/authority could not be proven. */
  scope_inconsistent: number;
  blocking: number;
  advisory: number;
  blocking_failed: number;
  blocking_pending: number;
  advisory_failed: number;
  advisory_pending: number;
  failed_metrics: number;
  waived_metrics: number;
  unwaived_failed_metrics: number;
}

export interface PolicyComplianceLifecycleMetric {
  metric_id: string;
  code: string;
  title: string;
  description: string;
  description_truncated: boolean;
  evaluation_rubric: string;
  evaluation_rubric_truncated: boolean;
  assessment_outcome: PolicyComplianceLifecycleMetricOutcome;
  direction: 'minimum' | 'maximum';
  default_threshold: number;
  effective_threshold: number;
  threshold_source: 'default' | 'override';
}

export interface PolicyComplianceLifecycleBinding {
  binding_id: string;
  guideline_id: string;
  /** Exact immutable guideline revision frozen for this validation edition. */
  revision_id: string;
  title: string;
  enforcement: 'advisory' | 'blocking';
  minimum_confidence: number;
  status: PolicyComplianceLifecycleBindingStatus;
  failed_metric_count: number;
  waived_metric_count: number;
  unwaived_failed_metric_count: number;
  metrics: PolicyComplianceLifecycleMetric[];
}

export interface PolicyComplianceLifecycleDetails {
  counts: PolicyComplianceLifecycleCounts;
  applicable_bindings: PolicyComplianceLifecycleBinding[];
}

export interface ValidationCycleCheckSummary {
  result_type:
    | 'requirement_lint'
    | 'curated_checklist'
    | 'policy_compliance';
  status: string;
  summary: string;
  /** Policy checks expose a frozen, edition-bound human projection here. */
  details: PolicyComplianceLifecycleDetails | Record<string, unknown>;
}

export type ValidationCycleVisibleSection =
  | 'ambiguity_assessment'
  | 'spec_validation'
  | 'requirement_lint'
  | 'curated_checklist'
  | 'policy_compliance';

interface ValidationCycleSummaryIdentity {
  subject_id: string;
  edition: number;
  subject_status: string;
  visible_sections: ValidationCycleVisibleSection[];
}

interface ValidationCycleSummaryBase extends ValidationCycleSummaryIdentity {
  cycle_state: ValidationCycleState;
  current_result: ValidationCycleResultSummary | null;
  previous_result_count: number;
  previous_results: ValidationCycleResultSummary[];
  submission_fence: ValidationSubmissionFence;
}

export interface QualityValidationCycleSummary
  extends ValidationCycleSummaryBase {
  subject_type: 'ideation' | 'refinement';
}

export interface SpecValidationCycleSummary
  extends ValidationCycleSummaryIdentity {
  subject_type: 'spec';
  /** Present only when the actor can read Spec Validation results. */
  cycle_state?: ValidationCycleState;
  /** Present only when the actor can read Spec Validation results. */
  current_result?: ValidationCycleResultSummary | null;
  /** Present only when the actor can read Spec Validation results. */
  previous_result_count?: number;
  /** Present only when the actor can read Spec Validation results. */
  previous_results?: ValidationCycleResultSummary[];
  /** Present only when the actor can read Spec Validation results. */
  submission_fence?: ValidationSubmissionFence;
  checks: ValidationCycleCheckSummary[];
  remaining_actions: string[];
}

export type ValidationCycleSummary =
  | QualityValidationCycleSummary
  | SpecValidationCycleSummary;

export interface ValidationTechnicalAudit {
  subject_type: QualitySubjectType;
  subject_id: string;
  result_id: string;
  result_type:
    | 'ambiguity_assessment'
    | 'spec_validation'
    | 'requirement_lint';
  /** Null identifies technical evidence attached to a legacy result. */
  subject_edition: number | null;
  technical_audit: {
    receipt_id: string;
    subject_version: number;
    head_revision: number;
    digests: Record<string, string>;
    visible_exception_types: Array<
      'ambiguity_gate_skip' | 'policy_skip' | 'policy_waiver'
    >;
    exceptions: Array<{
      exception_id: string;
      exception_type:
        | 'ambiguity_gate_skip'
        | 'policy_skip'
        | 'policy_waiver';
      subject_edition: number;
      status: string;
      reason: string;
      actor_id: string;
      recorded_at: string;
    }>;
  };
}

export interface RequirementLintSubmissionFence {
  expected_subject_edition: number;
  expected_subject_version: number;
  expected_head_revision: number;
}

export interface RequirementLintPreflight {
  assessment_kind: 'requirement_lint';
  subject_edition: number;
  subject_status: 'approved';
  ruleset_digest: string;
  requirement_anchors: Array<{
    anchor_type: string;
    anchor_ref: string | null;
    excerpt_hash: string | null;
  }>;
  submission_fence: RequirementLintSubmissionFence;
}

// Ideation Q&A (same structure as Spec Q&A)
export interface IdeationQAItem {
  id: string;
  ideation_id: string;
  question: string;
  question_type: 'text' | 'choice' | 'single_choice' | 'multi_choice';
  choices: { id: string; label: string }[] | null;
  allow_free_text: boolean;
  answer: string | null;
  selected: string[] | null;
  asked_by: string;
  answered_by: string | null;
  created_at: string;
  answered_at: string | null;
}

// Refinement Q&A
export interface RefinementQAItem {
  id: string;
  refinement_id: string;
  question: string;
  question_type: 'text' | 'choice' | 'single_choice' | 'multi_choice';
  choices: { id: string; label: string }[] | null;
  allow_free_text: boolean;
  answer: string | null;
  selected: string[] | null;
  asked_by: string;
  answered_by: string | null;
  created_at: string;
  answered_at: string | null;
}

// Ideation History (same structure as SpecHistory)
export interface IdeationHistoryEntry {
  id: string;
  ideation_id: string;
  action: string;
  actor_type: string;
  actor_id: string;
  actor_name: string;
  changes: { field: string; old: unknown; new: unknown }[] | null;
  summary: string | null;
  version: number | null;
  created_at: string;
}

// Refinement History
export interface RefinementHistoryEntry {
  id: string;
  refinement_id: string;
  action: string;
  actor_type: string;
  actor_id: string;
  actor_name: string;
  changes: { field: string; old: unknown; new: unknown }[] | null;
  summary: string | null;
  version: number | null;
  created_at: string;
}

// Ideation Snapshot
export interface IdeationSnapshot {
  id: string;
  ideation_id: string;
  version: number;
  title: string;
  description: string | null;
  problem_statement: string | null;
  proposed_approach: string | null;
  scope_assessment: { domains: number; ambiguity: number; dependencies: number } | null;
  complexity: string | null;
  labels: string[] | null;
  qa_snapshot: { question: string; answer: string | null; asked_by: string; answered_by: string | null }[] | null;
  created_by: string;
  created_at: string;
}

export interface IdeationSnapshotSummary {
  id: string;
  version: number;
  title: string;
  complexity: string | null;
  created_by: string;
  created_at: string;
}

// Ideation Knowledge Base
export interface IdeationKnowledge {
  id: string;
  ideation_id: string;
  title: string;
  description: string | null;
  content: string;
  mime_type: string;
  source_type?: string | null;
  source_id?: string | null;
  source_title?: string | null;
  source_version?: number | null;
  source_kb_id?: string | null;
  root_source_kb_id?: string | null;
  immediate_parent_kb_id?: string | null;
  content_hash?: string | null;
  governance_metadata?: unknown | null;
  governance?: Record<string, unknown>;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface IdeationKnowledgeSummary {
  id: string;
  ideation_id: string;
  title: string;
  description: string | null;
  mime_type: string;
  source_type?: string | null;
  source_id?: string | null;
  source_title?: string | null;
  source_version?: number | null;
  source_kb_id?: string | null;
  root_source_kb_id?: string | null;
  immediate_parent_kb_id?: string | null;
  content_hash?: string | null;
  governance_metadata?: unknown | null;
  governance?: Record<string, unknown>;
  created_at: string;
}

// Refinement Snapshot
export interface RefinementSnapshot {
  id: string;
  refinement_id: string;
  version: number;
  title: string;
  description: string | null;
  in_scope: string[] | null;
  out_of_scope: string[] | null;
  analysis: string | null;
  decisions: string[] | null;
  labels: string[] | null;
  qa_snapshot: { question: string; answer: string | null; asked_by: string; answered_by: string | null }[] | null;
  created_by: string;
  created_at: string;
}

export interface RefinementSnapshotSummary {
  id: string;
  version: number;
  title: string;
  created_by: string;
  created_at: string;
}

// Refinement Knowledge Base
export interface RefinementKnowledge {
  id: string;
  refinement_id: string;
  title: string;
  description: string | null;
  content: string;
  mime_type: string;
  source_type?: string;
  source_id?: string | null;
  source_title?: string | null;
  source_version?: number | null;
  source_kb_id?: string | null;
  root_source_kb_id?: string | null;
  immediate_parent_kb_id?: string | null;
  content_hash?: string | null;
  governance_metadata?: unknown | null;
  governance?: Record<string, unknown>;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface RefinementKnowledgeSummary {
  id: string;
  refinement_id: string;
  title: string;
  description: string | null;
  mime_type: string;
  source_type?: string;
  source_id?: string | null;
  source_title?: string | null;
  source_version?: number | null;
  source_kb_id?: string | null;
  root_source_kb_id?: string | null;
  immediate_parent_kb_id?: string | null;
  content_hash?: string | null;
  governance_metadata?: unknown | null;
  governance?: Record<string, unknown>;
  created_at: string;
}

// Refinement Summary (for nesting in Ideation)
export interface RefinementSummary {
  id: string;
  open_qa_count?: number;
  active_spec_count?: number;
  ideation_id: string;
  board_id: string;
  title: string;
  description: string | null;
  status: RefinementStatus;
  /** Human-facing review cycle; advances only when returning to Draft. */
  edition?: number;
  version: number;
  assignee_id: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
  labels: string[] | null;
  archived?: boolean;
  skip_ambiguity_gate?: boolean;
  skip_ambiguity_gate_edition?: number | null;
  quality_summaries?: QualitySummaryMap;
}

// Business Rule
export interface BusinessRule {
  id: string;
  title: string;
  rule: string;
  when: string;
  then: string;
  linked_requirements: string[] | null;
  linked_task_ids: string[] | null;
  status?: 'active' | 'superseded' | 'revoked';
  notes: string | null;
}

// Decision — formalized design choice on a spec (spec b66d2562)
export type DecisionStatus = 'active' | 'superseded' | 'revoked';

export interface Decision {
  id: string;
  title: string;
  rationale: string;
  context: string | null;
  alternatives_considered: string[] | null;
  supersedes_decision_id: string | null;
  linked_requirements: string[] | null;
  linked_task_ids: string[] | null;
  status: DecisionStatus;
  notes: string | null;
}

// API Contract
export interface ApiContract {
  id: string;
  method: string;
  path: string;
  description: string;
  request_body: Record<string, unknown> | null;
  response_success: Record<string, unknown> | null;
  response_errors: Array<Record<string, unknown>> | null;
  linked_requirements: string[] | null;
  linked_rules: string[] | null;
  linked_task_ids: string[] | null;
  status?: 'active' | 'superseded' | 'revoked';
  notes: string | null;
}

// Integration Requirement
export type IntegrationRequirementStatus = 'active' | 'superseded' | 'revoked';
export type IntegrationRequirementType =
  | 'api'
  | 'queue'
  | 'stored_procedure'
  | 'data_contract'
  | 'event'
  | 'file'
  | 'other';

export interface IntegrationRequirement {
  id: string;
  title: string;
  integration_type: IntegrationRequirementType;
  description: string;
  provider: string | null;
  consumer: string | null;
  contract_ref: string | null;
  endpoint: string | null;
  method: string | null;
  data_contract: Record<string, unknown> | null;
  linked_requirements: string[] | null;
  linked_api_contracts: string[] | null;
  linked_task_ids: string[] | null;
  status: IntegrationRequirementStatus;
  notes: string | null;
}

// Observability Requirement
export type ObservabilityRequirementStatus = 'active' | 'superseded' | 'revoked';
export type ObservabilitySignalType =
  | 'metric'
  | 'log'
  | 'trace'
  | 'dashboard'
  | 'alert'
  | 'slo'
  | 'other';

export interface ObservabilityRequirement {
  id: string;
  title: string;
  signal_type: ObservabilitySignalType;
  description: string;
  target: string | null;
  metric_name: string | null;
  threshold: string | null;
  severity: string | null;
  owner: string | null;
  linked_requirements: string[] | null;
  linked_integration_requirements: string[] | null;
  linked_task_ids: string[] | null;
  status: ObservabilityRequirementStatus;
  notes: string | null;
}

// Technical Requirement (structured)
export interface TechnicalRequirement {
  id: string;
  text: string;
  linked_task_ids: string[] | null;
  status?: 'active' | 'superseded' | 'revoked';
  notes?: string | null;
}

// Structured spec entity editing
export type SpecStructuredEntityType =
  | 'functional_requirement'
  | 'acceptance_criterion'
  | 'technical_requirement'
  | 'business_rule'
  | 'api_contract'
  | 'integration_requirement'
  | 'observability_requirement'
  | 'decision';

export type SpecStructuredEntityOperation =
  | 'create'
  | 'update'
  | 'revoke'
  | 'supersede'
  | 'restore'
  | 'reorder'
  | 'link_task'
  | 'unlink_task';

export interface SpecStructuredEntityImpactRef {
  target_type: string;
  target_id: string;
  target_ref: string;
  severity: string;
  reason: string;
  blocking: boolean;
}

export interface SpecStructuredEntityImpactReport {
  impacted_refs: SpecStructuredEntityImpactRef[];
  counts_by_type: Record<string, number>;
  ack_token?: string | null;
  expires_at?: string | null;
}

export interface SpecStructuredEntityMutationRequest {
  operation?: SpecStructuredEntityOperation;
  payload?: Record<string, unknown>;
  expected_spec_version?: number | null;
  task_id?: string | null;
  ack_token?: string | null;
}

export interface SpecStructuredEntityMutationResult {
  success: boolean;
  entity_type: SpecStructuredEntityType;
  operation: SpecStructuredEntityOperation;
  spec_id: string;
  entity_id: string | null;
  child_ref: string | null;
  spec_version: number | null;
  changed_fields: string[];
  error_code: string | null;
  error_message: string | null;
  required_permission: string | null;
  impact_report: SpecStructuredEntityImpactReport | null;
  ack_token: string | null;
  expires_at: string | null;
}

// Test Scenario
export type TestScenarioType =
  | 'unit'
  | 'integration'
  | 'e2e'
  | 'manual'
  | 'negative';
export type TestScenarioStatus = 'draft' | 'ready' | 'automated' | 'passed' | 'failed';

// Re-executable validation evidence contract (spec 9e0bf979).
export type EvidenceClass =
  | 'automated_test_pointer'
  | 'replay_command'
  | 'mcp_replay_manifest'
  | 'manual_checklist'
  | 'run_log'
  | 'non_replayable_justified';

export interface TestEvidenceAssertionV2 {
  name: string;
  expected: unknown;
  observed: unknown;
  status: 'passed' | 'failed';
  message?: string | null;
}

export interface TestEvidenceProvenanceV2 {
  producer: string;
  producer_version: string;
  adapter: string;
  environment: string;
}

export interface TestExecutionAttestationV2 {
  schema_version: 2;
  run_id: string;
  executed_at: string;
  scenario_id: string;
  outcome: 'passed' | 'failed';
  product_runtime_exercised: boolean;
  manifest_sha256: string;
  assertions: TestEvidenceAssertionV2[];
  provenance: TestEvidenceProvenanceV2;
  attestation_sha256: string;
}

export interface TestScenarioEvidence {
  // Legacy / minimal fields (NC-9).
  test_file_path?: string | null;
  test_function?: string | null;
  last_run_at?: string | null;
  test_run_id?: string | null;
  output_snippet?: string | null;
  // Re-executable evidence contract (spec 9e0bf979). All optional; legacy
  // evidence simply omits them.
  evidence_class?: EvidenceClass | null;
  replay_command?: string | null;
  /** @deprecated Reader-only legacy alias; it never satisfies Evidence V2. */
  mcp_replay_manifest?: string | Record<string, unknown> | null;
  manifest_ref?: string | null;
  execution_attestation?: TestExecutionAttestationV2 | null;
  /** Opaque receipt authenticated by the local installation at write time. */
  execution_receipt?: string | null;
  manual_checklist_ref?: string | null;
  expected_output_snapshot?: string | null;
  replay_should_exist?: boolean | null;
  non_replayable_justification?: string | null;
}

export interface TestScenario {
  id: string;
  title: string;
  linked_criteria: string[] | null;
  /** Raw read value; legacy rows may expose an unsupported historical type. */
  scenario_type: string;
  given: string;
  when: string;
  then: string;
  notes: string | null;
  status: TestScenarioStatus;
  linked_task_ids: string[] | null;
  created_at?: string;
  evidence?: TestScenarioEvidence | null;
  latest_evidence?: TestScenarioEvidence | null;
}

export interface TestScenarioStatusUpdateRequest {
  status: TestScenarioStatus;
  evidence?: TestScenarioEvidence | null;
}

export interface TestScenarioStatusUpdateResponse {
  id: string;
  scenario: {
    id: string;
    status: TestScenarioStatus;
  };
  result: {
    scenario_id: string;
    old_status: TestScenarioStatus;
    new_status: TestScenarioStatus;
    evidence_provided: boolean;
    evidence_gate_skipped: boolean;
    evidence_verification_status?: string;
  };
}

export type TestScenarioWrite = Omit<TestScenario, 'scenario_type'> & {
  /** Omission preserves an existing row and defaults a new row to integration. */
  scenario_type?: TestScenarioType;
};

// Screen Mockups
export interface MockupAnnotation {
  id: string;
  text: string;
  author_id: string | null;
}

export interface ScreenMockup {
  id: string;
  title: string;
  description: string | null;
  screen_type: 'page' | 'modal' | 'drawer' | 'popover' | 'panel';
  html_content: string;
  annotations: MockupAnnotation[] | null;
  order: number;
  origin_id?: string | null;
  origin_story_id?: string | null;
  origin_entity_type?: string | null;
  // Design System consumption metadata (spec 3a006f65 / card 0192f58d). Normalized
  // {design_system_id, version}; the server's MockupDesignSystemGate validates it
  // against the board's real effective Design System. Optional (legacy/off-mode).
  design_system_ref?: { design_system_id: string; version?: number | null } | null;
  design_system_evidence?: string | Record<string, unknown> | null;
}

export type StoryStatus = 'draft' | 'triage' | 'ready' | 'converted';

export const STORY_STATUSES: StoryStatus[] = ['draft', 'triage', 'ready', 'converted'];

export const STORY_STATUS_LABELS: Record<StoryStatus, string> = {
  draft: 'Draft',
  triage: 'Triage',
  ready: 'Ready',
  converted: 'Converted',
};

export interface Topic {
  id: string;
  board_id: string;
  name: string;
  description: string | null;
  archived: boolean;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface TopicSummary extends Topic {
  story_count: number;
  active_count?: number;
  archived_count?: number;
  total_associated_count?: number;
}

export interface TopicDeleteResponse {
  success: boolean;
  deleted_topic_id: string;
}

export interface TopicMergeRequest {
  target_topic_id: string;
}

export interface TopicMergeResponse {
  success: boolean;
  source: TopicSummary;
  target: TopicSummary;
  moved_count: number;
  active_count: number;
  archived_count: number;
  target_total_before: number;
  target_total_after: number;
}

export interface StoryIdeationLink {
  id: string;
  board_id: string;
  story_id: string;
  ideation_id: string;
  created_by: string;
  created_at: string;
}

export interface StorySummary {
  id: string;
  board_id: string;
  topic_id: string;
  title: string;
  description: string;
  actor: string | null;
  goal: string | null;
  benefit: string | null;
  labels: string[] | null;
  status: StoryStatus;
  assignee_id: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
  archived: boolean;
  pre_archive_status: string | null;
  screen_mockups: ScreenMockup[] | null;
  ideation_links: StoryIdeationLink[];
}

export interface Story extends StorySummary {
  topic: Topic | null;
}

// Architecture Design
export type ArchitectureParentType = 'ideation' | 'refinement' | 'spec' | 'card';
export type ArchitectureDiagramType =
  | 'context'
  | 'container'
  | 'component'
  | 'sequence'
  | 'deployment'
  | 'data_flow'
  | 'other';
export type ArchitectureDiagramFormat =
  | 'excalidraw_json'
  | 'mermaid'
  | 'svg'
  | 'plantuml'
  | 'c4'
  | 'raw';

export interface ArchitectureEntity {
  id?: string | null;
  name: string;
  entity_type?: string | null;
  responsibility?: string | null;
  boundaries?: string | null;
  technologies?: string[];
  relationships?: string[];
  notes?: string | null;
}

export interface ArchitectureInterface {
  id?: string | null;
  name: string;
  endpoint?: string | null;
  description?: string | null;
  participants?: string[];
  direction?: string | null;
  protocol?: string | null;
  contract_type?: string | null;
  request_schema?: Record<string, unknown> | null;
  response_schema?: Record<string, unknown> | null;
  event_schema?: Record<string, unknown> | null;
  error_contract?: Record<string, unknown> | Record<string, unknown>[] | string | null;
  schema_ref?: string | null;
  notes?: string | null;
}

export interface ArchitectureDiagram {
  id?: string | null;
  title: string;
  diagram_type: ArchitectureDiagramType;
  format: ArchitectureDiagramFormat;
  adapter_payload_ref?: string | null;
  adapter_payload?: Record<string, unknown> | unknown[] | string | null;
  description?: string | null;
  order_index: number;
  content_hash?: string | null;
  preview_ref?: string | null;
  render_metadata?: Record<string, unknown> | null;
  size_bytes?: number | null;
  source_diagram_id?: string | null;
  source_payload_ref?: string | null;
}

export interface ArchitectureDesignSummary {
  id: string;
  board_id: string;
  parent_type: ArchitectureParentType;
  parent_id: string;
  title: string;
  version: number;
  source_ref?: string | null;
  source_version?: number | null;
  source_design_id?: string | null;
  stale: boolean;
  breaking_change_flag: boolean;
  requires_arch_review: boolean;
  diagrams_count: number;
  adapter_payload_refs: string[];
  created_at: string;
  updated_at: string;
}

export interface ArchitectureDesign {
  id: string;
  board_id: string;
  parent_type: ArchitectureParentType;
  parent_id: string;
  title: string;
  global_description: string;
  entities: ArchitectureEntity[];
  interfaces: ArchitectureInterface[];
  diagrams: ArchitectureDiagram[];
  version: number;
  source_ref?: string | null;
  source_version?: number | null;
  source_design_id?: string | null;
  stale: boolean;
  breaking_change_flag: boolean;
  requires_arch_review: boolean;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface ArchitectureDiagramPayloadResponse {
  design_id: string;
  diagram_id: string;
  format: ArchitectureDiagramFormat;
  content_hash: string;
  size_bytes: number;
  payload: Record<string, unknown> | unknown[] | string | null;
}

export type CreateArchitectureDesignRequest = Pick<
  ArchitectureDesign,
  'title' | 'global_description' | 'entities' | 'interfaces' | 'diagrams'
> & Partial<Pick<ArchitectureDesign, 'source_ref' | 'source_version' | 'source_design_id'>> & {
  design_id?: string;
  architecture_warning_acknowledgement?: ArchitectureWarningAcknowledgementRequest | null;
};

export type UpdateArchitectureDesignRequest = Partial<CreateArchitectureDesignRequest> & {
  change_summary?: string;
};

export interface ArchitectureWarningAcknowledgementRequest {
  accepted: boolean;
  warning_keys?: string[];
  statement?: string | null;
}

export interface ArchitectureDesignValidationResult {
  valid: boolean;
  issues: string[];
  warnings: string[];
  structured_warnings?: ArchitectureWarningRecord[];
  suppressed_warnings?: ArchitectureWarningRecord[];
  suggested_fixes: string[];
  summary: Record<string, unknown>;
}

export interface ArchitectureWarningRecord {
  code: string;
  severity: 'warning';
  message: string;
  path: string;
  suggested_fix: string;
  diagram_id?: string | null;
  diagram_type?: string | null;
  element_id?: string | null;
  entity_id?: string | null;
  node_ref?: string | null;
  justification?: string | null;
  finding_key?: string | null;
}

export interface CardKnowledgeBase {
  id: string;
  title: string;
  description: string | null;
  content: string;
  mime_type: string;
  source: 'manual' | 'spec';
  source_id?: string;
}

// Selective Knowledge Base propagation v2
export type KnowledgeSelectionState = 'omitted' | 'explicit_empty' | 'explicit_ids';
export type KnowledgePropagationMode = 'reference' | 'snapshot' | 'drop';
export type KnowledgeAssignmentState =
  | 'active'
  | 'stale'
  | 'source_deleted'
  | 'dropped'
  | 'inactive';
export type KnowledgeOriginClass =
  | 'v2'
  | 'legacy_all'
  | 'selected_legacy'
  | 'legacy_unresolved';
export type KnowledgeRelevanceEntityType =
  | 'functional_requirement'
  | 'acceptance_criterion'
  | 'test_scenario';

export interface KnowledgeRelevanceLinkRequest {
  entity_type: KnowledgeRelevanceEntityType;
  entity_id: string;
}

/**
 * Authoritative v2 selection envelope. Omitting the envelope itself preserves
 * the legacy v1 path; `selection_state: 'omitted'` is a distinct persisted v2
 * decision.
 */
export interface KnowledgePropagationEnvelopeV2 {
  contract_version?: 2;
  selection_state: KnowledgeSelectionState;
  mode?: KnowledgePropagationMode | null;
  knowledge_ids?: string[];
  justification?: string | null;
  idempotency_key: string;
  expected_revision?: 0 | null;
  relevance_links?: KnowledgeRelevanceLinkRequest[];
}

export interface DeriveSpecKnowledgeRequest {
  knowledge_propagation: KnowledgePropagationEnvelopeV2;
}

export interface KnowledgeAssignmentReplaceRequest {
  contract_version?: 2;
  knowledge_ids: string[];
  mode: Extract<KnowledgePropagationMode, 'reference' | 'snapshot'>;
  justification: string;
  idempotency_key: string;
  expected_revision: number;
  linkage?: KnowledgeRelevanceLinkRequest[];
}

export interface KnowledgeAssignmentDropRequest {
  contract_version?: 2;
  knowledge_ids?: string[];
  justification: string;
  idempotency_key: string;
  expected_revision: number;
}

export interface KnowledgeAssignmentRefreshRequest {
  contract_version?: 2;
  knowledge_ids: string[];
  idempotency_key: string;
  expected_revision: number;
}

export interface KnowledgeMutationAssignmentResponse {
  root_knowledge_id: string;
  source_knowledge_id: string;
  mode: KnowledgePropagationMode;
  state: KnowledgeAssignmentState;
  stale: boolean;
}

export interface KnowledgeMutationResponse {
  contract_version: 2;
  target_type: 'spec' | 'card';
  target_id: string;
  operation_id: string;
  revision: number;
  replayed: boolean;
  selection_state: KnowledgeSelectionState;
  assignments: KnowledgeMutationAssignmentResponse[];
}

export interface DeriveSpecKnowledgeResponse extends KnowledgeMutationResponse {
  target_type: 'spec';
  spec_id: string;
}

export interface CardCreateKnowledgeMutationResponse {
  contract_version: 2;
  card: Card;
  operation_id: string;
  revision: number;
  replayed: boolean;
  selection_state: KnowledgeSelectionState;
  assignments: KnowledgeMutationAssignmentResponse[];
}

export interface KnowledgeRefreshItemResponse {
  root_knowledge_id: string;
  source_revision: string;
  source_content_sha256: string;
  stale: false;
}

export interface KnowledgeRefreshResponse {
  contract_version: 2;
  operation_id: string;
  revision: number;
  replayed: boolean;
  refreshed: KnowledgeRefreshItemResponse[];
}

export interface KnowledgeAssignmentTechnicalProjection {
  root_knowledge_id: string;
  mode: KnowledgePropagationMode;
  origin_class: KnowledgeOriginClass;
  state: KnowledgeAssignmentState;
  stale: boolean;
}

export interface KnowledgeTechnicalReadResponse {
  contract_version: 2;
  revision: number;
  selection_state: KnowledgeSelectionState | null;
  assignments: KnowledgeAssignmentTechnicalProjection[];
}

// Spec History
export interface SpecHistoryChange {
  field: string;
  old: unknown;
  new: unknown;
}

export interface SpecHistoryEntry {
  id: string;
  spec_id: string;
  action: string;
  actor_type: string;
  actor_id: string;
  actor_name: string;
  changes: SpecHistoryChange[] | null;
  summary: string | null;
  /** Technical revision captured by this history entry, not the human edition. */
  version: number | null;
  created_at: string;
}

// Spec Q&A
export interface SpecQAChoiceOption {
  id: string;
  label: string;
}

export interface SpecQAItem {
  id: string;
  spec_id: string;
  question: string;
  question_type: 'text' | 'choice' | 'single_choice' | 'multi_choice';
  choices: SpecQAChoiceOption[] | null;
  allow_free_text: boolean;
  answer: string | null;
  selected: string[] | null;
  asked_by: string;
  answered_by: string | null;
  created_at: string;
  answered_at: string | null;
}

// Spec Knowledge Base
export interface SpecKnowledge {
  id: string;
  spec_id: string;
  title: string;
  description: string | null;
  content: string;
  mime_type: string;
  source_type?: string | null;
  source_id?: string | null;
  source_title?: string | null;
  source_version?: number | null;
  source_kb_id?: string | null;
  root_source_kb_id?: string | null;
  immediate_parent_kb_id?: string | null;
  content_hash?: string | null;
  governance_metadata?: unknown | null;
  governance?: Record<string, unknown>;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface SpecKnowledgeSummary {
  id: string;
  spec_id: string;
  title: string;
  description: string | null;
  mime_type: string;
  source_type?: string | null;
  source_id?: string | null;
  source_title?: string | null;
  source_version?: number | null;
  source_kb_id?: string | null;
  root_source_kb_id?: string | null;
  immediate_parent_kb_id?: string | null;
  content_hash?: string | null;
  governance_metadata?: unknown | null;
  governance?: Record<string, unknown>;
  created_at: string;
}

// Spec
export interface Spec extends TaskValidationGateOverride {
  id: string;
  board_id: string;
  ideation_id: string | null;
  refinement_id: string | null;
  title: string;
  description: string | null;
  context: string | null;
  functional_requirements: string[] | null;
  technical_requirements: (string | TechnicalRequirement)[] | null;
  acceptance_criteria: string[] | null;
  test_scenarios: TestScenario[] | null;
  business_rules: BusinessRule[] | null;
  api_contracts: ApiContract[] | null;
  integration_requirements: IntegrationRequirement[] | null;
  observability_requirements: ObservabilityRequirement[] | null;
  decisions: Decision[] | null;
  screen_mockups: ScreenMockup[] | null;
  architecture_designs?: ArchitectureDesignSummary[];
  skip_test_coverage: boolean;
  skip_code_evidence_coverage: boolean;
  skip_rules_coverage?: boolean;
  skip_decisions_coverage?: boolean;
  skip_contract_coverage?: boolean;
  skip_ir_coverage?: boolean;
  skip_or_coverage?: boolean;
  skip_qualitative_validation?: boolean;
  validation_threshold?: number;
  archived?: boolean;
  pre_archive_status?: string | null;
  // Cancellation justification (set only while status === 'cancelled')
  cancellation_reason?: string | null;
  cancelled_at?: string | null;
  cancelled_by?: string | null;
  status: SpecStatus;
  /** Human-facing lifecycle counter; changes only when the Spec re-enters draft. */
  edition: number;
  /** Internal technical revision used for CAS, receipts, and currentness. */
  version: number;
  assignee_id: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
  labels: string[] | null;
  cards: CardSummaryForSpec[];
  knowledge_bases: SpecKnowledgeSummary[];
  qa_items: SpecQAItem[];
  quality_summaries?: QualitySummaryMap;
  /** Authoritative precedence gate projection used by the modal header. */
  dependency_readiness?: import('./spec-dependencies').SpecDependencyReadiness;
}

// Spec summary (without nested cards)
export interface SpecSummary {
  id: string;
  open_qa_count?: number;
  board_id: string;
  ideation_id: string | null;
  refinement_id: string | null;
  title: string;
  description: string | null;
  status: SpecStatus;
  /** Human-facing lifecycle counter; changes only when the Spec re-enters draft. */
  edition: number;
  /** Internal technical revision used for CAS, receipts, and currentness. */
  version: number;
  assignee_id: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
  labels: string[] | null;
  architecture_designs?: ArchitectureDesignSummary[];
  archived?: boolean;
  quality_summaries?: QualitySummaryMap;
  dependency_readiness?: import('./spec-dependencies').SpecDependencyReadiness;
}

// Ideation
export interface Ideation {
  id: string;
  board_id: string;
  title: string;
  description: string | null;
  problem_statement: string | null;
  proposed_approach: string | null;
  scope_assessment: { domains: number; ambiguity: number; dependencies: number } | null;
  complexity: IdeationComplexity | null;
  screen_mockups: ScreenMockup[] | null;
  architecture_designs?: ArchitectureDesignSummary[];
  status: IdeationStatus;
  /** Human-facing review cycle; advances only when returning to Draft. */
  edition?: number;
  version: number;
  assignee_id: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
  labels: string[] | null;
  archived?: boolean;
  pre_archive_status?: string | null;
  // Per-ideation opt-out of the board Max ambiguity gate (spec 2485780b).
  skip_ambiguity_gate?: boolean;
  skip_ambiguity_gate_edition?: number | null;
  // Cancellation justification (set only while status === 'cancelled')
  cancellation_reason?: string | null;
  cancelled_at?: string | null;
  cancelled_by?: string | null;
  refinements: RefinementSummary[];
  stories: StorySummary[];
  specs: SpecSummary[];
  knowledge_bases: IdeationKnowledgeSummary[];
  qa_items: IdeationQAItem[];
  quality_summaries?: QualitySummaryMap;
}

export interface IdeationSummary {
  id: string;
  // Evaluation scores (present after evaluate_ideation) — rendered as score badges.
  scope_assessment?: { domains: number; ambiguity: number; dependencies: number } | null;
  // Unanswered Q&A count (answered_at IS NULL) — drives the "open Q&A" badge.
  open_qa_count?: number;
  // Non-archived, non-cancelled child refinements — drives the "No refinement" badge.
  active_refinement_count?: number;
  // Non-archived, non-cancelled direct specs — drives the "No spec" badge for small ideations.
  active_spec_count?: number;
  board_id: string;
  title: string;
  description: string | null;
  problem_statement: string | null;
  complexity: IdeationComplexity | null;
  status: IdeationStatus;
  /** Human-facing review cycle; advances only when returning to Draft. */
  edition?: number;
  version: number;
  assignee_id: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
  labels: string[] | null;
  architecture_designs?: ArchitectureDesignSummary[];
  archived?: boolean;
  quality_summaries?: QualitySummaryMap;
}

// Refinement (full)
export interface Refinement {
  id: string;
  ideation_id: string;
  board_id: string;
  title: string;
  description: string | null;
  in_scope: string[] | null;
  out_of_scope: string[] | null;
  analysis: string | null;
  decisions: string[] | null;
  screen_mockups: ScreenMockup[] | null;
  architecture_designs?: ArchitectureDesignSummary[];
  status: RefinementStatus;
  /** Human-facing review cycle; advances only when returning to Draft. */
  edition?: number;
  version: number;
  assignee_id: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
  labels: string[] | null;
  archived?: boolean;
  pre_archive_status?: string | null;
  // Human-authorized opt-out of the board refinement ambiguity gate.
  skip_ambiguity_gate?: boolean;
  skip_ambiguity_gate_edition?: number | null;
  // Cancellation justification (set only while status === 'cancelled')
  cancellation_reason?: string | null;
  cancelled_at?: string | null;
  cancelled_by?: string | null;
  specs: SpecSummary[];
  qa_items: RefinementQAItem[];
  knowledge_bases: RefinementKnowledgeSummary[];
  quality_summaries?: QualitySummaryMap;
}

export interface RefinementAmbiguityGateSkipRequest {
  skip_ambiguity_gate: boolean;
  reason: string;
  expected_refinement_version: number;
  expected_refinement_edition: number;
}

export interface RefinementAmbiguityGateSkipReceipt {
  skipped: boolean;
  activity_id: string;
  version: number;
  edition?: number;
}

// Card
export interface Card {
  id: string;
  board_id: string;
  subject_version?: number;
  spec_id: string | null;
  sprint_id: string | null;
  title: string;
  description: string | null;
  details: string | null;
  status: CardStatus;
  priority: CardPriority;
  position: number;
  assignee_id: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
  due_date: string | null;
  labels: string[] | null;
  test_scenario_ids: string[] | null;
  screen_mockups: ScreenMockup[] | null;
  knowledge_bases: CardKnowledgeBase[] | null;
  conclusions: ConclusionEntry[] | null;
  attachments: Attachment[];
  qa_items: QAItem[];
  open_qa_count?: number | null;
  comments: Comment[];
  architecture_designs?: ArchitectureDesignSummary[];
  // Bug card fields (optional for backwards compat with existing cards)
  card_type?: CardType;
  origin_task_id?: string | null;
  severity?: BugSeverity | null;
  expected_behavior?: string | null;
  observed_behavior?: string | null;
  steps_to_reproduce?: string | null;
  action_plan?: string | null;
  linked_test_task_ids?: string[] | null;
  skip_task_requirement_link_gate?: boolean;
  validations?: ValidationEntry[] | null;
  rejection_records?: CardRejectionRecord[] | null;
  current_rejection_kind?: CardRejectionKind | null;
  current_rejection_id?: string | null;
  current_rejection_code?: string | null;
  current_rejection_summary?: string | null;
  // Cancellation justification (set only while status === 'cancelled')
  cancellation_reason?: string | null;
  cancelled_at?: string | null;
  cancelled_by?: string | null;
}

export type TaskValidationRecommendation = 'approve' | 'reject';
export type TaskValidationOutcome = 'success' | 'failed';
export type TaskValidationVerdict = 'pass' | 'fail';

/**
 * Payload accepted by POST /cards/{card_id}/validate.
 *
 * The API deliberately keeps the `estimated_*` names for the reviewer scores.
 * Do not send the clean read aliases (`completeness`, `drift`, `verdict`,
 * `summary`) here: those are response/history compatibility fields.
 */
export interface TaskValidationSubmitPayload {
  expected_subject_version: number;
  idempotency_key: string;
  confidence: number;
  confidence_justification: string;
  estimated_completeness: number;
  completeness_justification: string;
  estimated_drift: number;
  drift_justification: string;
  general_justification: string;
  recommendation: TaskValidationRecommendation;
}

export type CardRejectionKind = 'task_validation' | 'completion_gate';

export interface CardRejectionCause {
  kind: CardRejectionKind;
  id: string;
  code: string;
  summary: string;
}

export interface CardRejectionRecord extends CardRejectionCause {
  card_id?: string;
  board_id?: string;
  source_id?: string | null;
  reason_codes?: string[];
  created_by?: string;
  created_at?: string;
  subject_version?: number;
}

export interface TaskValidationResolvedThresholds {
  required?: boolean;
  min_confidence: number;
  min_completeness: number;
  max_drift: number;
  resolved_from?: 'sprint' | 'spec' | 'board' | 'default';
  resolved_sources?: {
    required: 'sprint' | 'spec' | 'board' | 'default';
    min_confidence: 'sprint' | 'spec' | 'board' | 'default';
    min_completeness: 'sprint' | 'spec' | 'board' | 'default';
    max_drift: 'sprint' | 'spec' | 'board' | 'default';
  };
  /**
   * The submit response can contain the complete resolved board gate config.
   * Keep additional settings readable without weakening the canonical scores.
   */
  [key: string]: unknown;
}

export interface TaskValidationReviewerSeparation {
  mode: 'off' | 'warn' | 'enforce';
  allowed: boolean;
  warning: boolean;
  conflicts: string[];
  source: string;
}

/**
 * Task-validation history entry.
 *
 * Current writes persist both the legacy API names and the clean UI aliases.
 * All aliases remain optional because cards created before the dual-write
 * migration may carry only one side of each pair.
 */
export interface ValidationEntry {
  id: string;
  card_id?: string;
  board_id?: string;

  // Reviewer identity: legacy name + clean UI alias.
  reviewer_id?: string | null;
  evaluator_id?: string | null;
  reviewer_name?: string | null;
  evaluator_name?: string | null;

  confidence: number;
  confidence_justification?: string | null;

  // Completeness: legacy name + clean UI alias.
  estimated_completeness?: number;
  completeness?: number;
  completeness_justification?: string | null;

  // Drift: legacy name + clean UI alias.
  estimated_drift?: number;
  drift?: number;
  drift_justification?: string | null;

  // General rationale: legacy name + clean UI alias.
  general_justification?: string | null;
  summary?: string | null;

  recommendation?: TaskValidationRecommendation;
  outcome?: TaskValidationOutcome;
  verdict?: TaskValidationVerdict;
  threshold_violations?: string[];
  resolved_thresholds?: TaskValidationResolvedThresholds | null;
  reviewer_separation?: TaskValidationReviewerSeparation | null;
  expected_subject_version?: number;
  idempotency_key?: string;
  validation_outcome?: TaskValidationOutcome;
  completion_outcome?: 'completed' | 'rejected';
  completion_gate_failures?: Array<{
    code: string;
    summary: string;
    reason_codes?: string[];
  }>;
  rejection_cause?: CardRejectionCause | null;
  subject_version?: number;
  replayed?: boolean;

  created_at: string;
  card_status?: CardStatus | null;
}

/** Semantic alias for callers that expose the submit endpoint response. */
export type TaskValidationResponse = ValidationEntry;

// Card for column view (simplified)
export interface CardSummary {
  id: string;
  open_qa_count?: number | null;
  board_id: string;
  spec_id: string | null;
  title: string;
  description: string | null;
  status: CardStatus;
  priority: CardPriority;
  position: number;
  assignee_id: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
  due_date: string | null;
  labels: string[] | null;
  test_scenario_ids: string[] | null;
  conclusions: ConclusionEntry[] | null;
  architecture_designs?: ArchitectureDesignSummary[];
  validations?: ValidationEntry[] | null;
  // Projection fields are present on paginated responses but remain optional
  // so full Card records and legacy fixtures can share the display surface.
  card_type?: CardType;
  origin_task_id?: string | null;
  severity?: BugSeverity | null;
  linked_test_task_ids?: string[] | null;
  skip_task_requirement_link_gate?: boolean;
  archived?: boolean;
  current_rejection_kind?: CardRejectionKind | null;
  current_rejection_id?: string | null;
  current_rejection_code?: string | null;
  current_rejection_summary?: string | null;
}

export interface KanbanColumnMeta {
  total_filtered: number;
  total_overall: number;
  has_more: boolean;
  facets: {
    card_type: Partial<Record<CardType, number>>;
  };
}

export interface KanbanColumnsMeta {
  columns: Record<CardStatus, KanbanColumnMeta>;
  facets: {
    assignee: Array<{ value: string | null; count: number }>;
  };
}

export interface ColumnsOptInResponse {
  board_id: string;
  columns: Record<CardStatus, CardSummary[]>;
  columns_meta: KanbanColumnsMeta;
}

export interface ColumnPageResponse {
  board_id: string;
  column: CardStatus;
  items: CardSummary[];
  meta: KanbanColumnMeta;
  offset: number;
  limit: number;
  next_offset: number | null;
}

export interface LookupOption {
  id: string;
  title: string;
  status: string;
}

export interface LookupPage {
  items: LookupOption[];
  total: number;
  offset: number;
  limit: number;
}

// Permission flags are backend-owned and may introduce groups at arbitrary
// depth (for example spec.structured_entity.<type>.<action>).
export type PermissionFlagTree = {
  [key: string]: boolean | PermissionFlagTree;
};
export type PermissionFlags = Record<string, PermissionFlagTree>;

// Permission Preset
export interface PermissionPreset {
  id: string;
  owner_id: string | null;
  name: string;
  description: string | null;
  is_builtin: boolean;
  base_preset_id: string | null;
  flags: PermissionFlags;
  owner_review_required: boolean;
  review_reason: string | null;
  created_at: string;
  updated_at: string | null;
}

// Agent (global, secret-free; credentials are reveal-once responses)
export interface Agent {
  id: string;
  name: string;
  description: string | null;
  objective: string | null;
  is_active: boolean;
  permissions: string[] | null;
  permission_flags: PermissionFlags | null;
  preset_id: string | null;
  created_by: string;
  created_at: string;
  last_used_at: string | null;
}

export interface AgentRevealResponse {
  agent: Agent;
  reveal_once_secret: string;
  message: string | null;
}

// Agent summary (without sensitive data, used in board context)
export interface AgentSummary {
  id: string;
  name: string;
  description: string | null;
  objective: string | null;
  is_active: boolean;
  preset_id?: string | null;
  /** Sparse direct delta from the selected preset / Full Control base. */
  permission_flags?: Record<string, unknown> | null;
  /** Raw board ceiling; distinct from permission_flags and effective values. */
  permission_overrides?: Record<string, unknown> | null;
  created_at: string;
  last_used_at: string | null;
}

// Agent-Board access grant
export interface AgentBoardGrant {
  id: string;
  agent_id: string;
  board_id: string;
  granted_by: string;
  granted_at: string;
  permission_overrides?: Record<string, unknown> | null;
}

// Board
export type SpecResourceAutoDeriveType = 'knowledge_base' | 'architecture' | 'mockup';
export type ReviewerSeparationMode = 'off' | 'warn' | 'enforce';

export interface CodeTraceabilitySettings {
  mode: 'off' | 'advisory' | 'blocking';
  evidence_attestation: 'none' | 'preferred' | 'required';
  target_resolution: 'advisory' | 'required' | 'required_current_receipt';
  accepted_attestor_policy:
    | 'granular_permission'
    | 'granular_permission_and_board_allowlist';
  minimum_trust: 'single_attestation' | 'corroborated';
  preflight_freshness_seconds: number;
  overlap_policy: 'off' | 'warn' | 'block_parallel';
  observed_state_policy:
    | 'allow_dirty_attestation'
    | 'require_committed_attestation';
  receipt_content: 'metadata_only' | 'safe_excerpt';
}

export type CodeTraceabilitySubjectType = 'refinement' | 'spec' | 'card';
export type CodeTraceabilityProfile = 'summary' | 'detail' | 'full';
export type CodeTraceabilityReceiptCurrentness =
  | 'current'
  | 'outdated'
  | 'expired'
  | 'revoked'
  | 'conflicted'
  | 'unknown';

export interface CodeTraceabilityWorkspaceState {
  workspace_state_id: string;
  declared_revision: string | null;
  declared_dirty: boolean;
  observed_at: string;
  reproducibility_claim: string;
  fingerprint_algorithm: string;
  manifest_digest: string;
  manifest_entry_count: number;
}

export interface CodeInvestigationReceipt {
  id: string;
  request_id: string;
  board_id: string;
  subject_type: CodeTraceabilitySubjectType;
  subject_id: string;
  subject_version: number;
  attestor_actor_id: string;
  generation: number;
  predecessor_receipt_id: string | null;
  trust_level: 'single_attestation' | 'corroborated' | 'conflicted' | string;
  acceptance_status: 'accepted' | 'rejected' | string;
  outcome: 'accessible' | 'partial' | 'unavailable';
  capabilities: string[];
  source_ref: string;
  source_identity_digest: string | null;
  canonicalization_profile: string;
  limits_profile: string;
  selector_scope_digest: string;
  declared_revision: string | null;
  workspace_state: CodeTraceabilityWorkspaceState | null;
  omission_manifest: Array<{
    reason_code: string;
    affected_scope_digest: string;
    count: number;
  }>;
  omission_digest: string;
  omission_count: number;
  tooling: {
    tool_id?: string;
    tool_version?: string;
    method_id?: string;
    [key: string]: unknown;
  };
  observed_at: string;
  received_at: string;
  expires_at: string;
  observation_sha256: string;
  payload_sha256: string;
}

export interface CodeInvestigationReceiptReadResult {
  receipt: CodeInvestigationReceipt;
  currentness: CodeTraceabilityReceiptCurrentness;
}

export interface CodeTraceabilityEvidence {
  id: string;
  investigation_receipt_id: string;
  source_ref: string;
  parent_type: CodeTraceabilitySubjectType;
  parent_id: string;
  parent_version: number;
  evidence_type: string;
  claim?: string | null;
  workspace_state?: CodeTraceabilityWorkspaceState | null;
  selector_kind: string;
  relative_path: string | null;
  language: string | null;
  symbol_kind: string | null;
  qualified_symbol: string | null;
  symbol_signature?: string | null;
  snapshot_line_start?: number | null;
  snapshot_line_end?: number | null;
  excerpt?: string | null;
  excerpt_truncated?: boolean | null;
  attestation_state: 'agent_attested' | 'agent_attested_worktree' | string;
  lifecycle_status: 'active' | 'superseded' | 'revoked' | string;
  supersedes_evidence_id: string | null;
  revocation_reason?: string | null;
  submitted_by?: string | null;
  received_at?: string | null;
}

export interface CodeEvidenceRevokeRequest {
  reason: string;
}

export interface CodeTraceabilityEvidenceLink {
  id: string;
  evidence_id: string;
  spec_id: string;
  entity_type: string;
  entity_id: string;
  relation_type: string;
  rationale?: string;
}

export interface CodeTraceabilityDisposition {
  id: string;
  evidence_id: string;
  disposition: string;
  justification?: string;
  active: boolean;
}

export interface ImplementationTargetResolution {
  id: string;
  target_id: string;
  investigation_receipt_id: string;
  receipt_generation: number;
  subject_version: number;
  target_revision: number;
  state: string;
  resolved_relative_path: string | null;
  resolved_qualified_symbol: string | null;
  resolved_line_start?: number | null;
  resolved_line_end?: number | null;
  confidence: number | null;
  received_at?: string;
}

export type ImplementationTargetSelectorKind =
  | 'symbol'
  | 'file'
  | 'glob'
  | 'semantic'
  | 'new_file';

export type ImplementationTargetRole =
  | 'read'
  | 'modify'
  | 'extend'
  | 'create'
  | 'delete'
  | 'test'
  | 'validate';

export interface ImplementationTargetCreateRequest {
  source_ref: string;
  selector_kind: ImplementationTargetSelectorKind;
  relative_path_hint: string | null;
  language: string | null;
  symbol_kind: string | null;
  qualified_symbol: string | null;
  symbol_signature: string | null;
  role: ImplementationTargetRole;
  intent: string;
  required: boolean;
  expected_spec_version: number;
  baseline_evidence_id: string | null;
  spec_links: Array<{ entity_type: string; entity_id: string }>;
  evidence_links: Array<{ evidence_id: string; relation_type: string }>;
}

export type TargetOverlapDisposition =
  | 'ordered_by_dependency'
  | 'accepted_parallel'
  | 'merged_targets'
  | 'false_positive';

export interface TargetOverlapAcknowledgementRequest {
  target_a_id: string;
  target_b_id: string;
  resolution_a_id: string;
  resolution_b_id: string;
  disposition: TargetOverlapDisposition;
  justification: string;
}

export type CodeTraceabilityWaiverScope =
  | 'implementation_target'
  | 'target_resolution'
  | 'target_overlap';

export type CodeTraceabilityWaiverReason =
  | 'no_code_change'
  | 'documentation_only'
  | 'manual_process'
  | 'external_source_unavailable'
  | 'conceptual_board'
  | 'runtime_only'
  | 'other';

export interface CodeTraceabilityWaiver {
  id: string;
  board_id: string;
  entity_type: 'refinement' | 'spec' | 'card' | 'spec_entity';
  entity_id: string;
  scope: CodeTraceabilityWaiverScope | 'code_evidence' | 'evidence_linkage';
  reason_code: CodeTraceabilityWaiverReason;
  justification: string;
  active: boolean;
  created_by: string;
  created_at: string;
  cleared_by: string | null;
  cleared_at: string | null;
}

export interface CodeTraceabilityWaiverCreateRequest {
  entity_type: 'card';
  entity_id: string;
  scope: CodeTraceabilityWaiverScope;
  reason_code: CodeTraceabilityWaiverReason;
  justification: string;
}

export interface ImplementationTargetProjection {
  id: string;
  card_id: string;
  source_ref: string;
  selector_kind: string;
  relative_path_hint: string | null;
  qualified_symbol: string | null;
  role: 'read' | 'modify' | 'create' | 'delete' | string;
  intent?: string;
  required: boolean;
  lifecycle_status: string;
  revision: number;
  current_resolution_id: string | null;
}

export type ImplementationTargetExecutionDisposition =
  | 'touched'
  | 'not_touched'
  | 'replaced'
  | 'created'
  | 'deleted'
  | 'superseded';

export interface ImplementationTargetExecutionRecordProjection {
  id: string;
  board_id?: string;
  card_id: string;
  target_id: string;
  target_revision: number;
  result_investigation_receipt_id: string;
  disposition: ImplementationTargetExecutionDisposition;
  source_ref: string;
  result_declared_revision?: string | null;
  result_workspace_state_id?: string | null;
  actual_relative_path?: string | null;
  actual_qualified_symbol?: string | null;
  replacement_target_id?: string | null;
  justification?: string;
  submitted_by?: string;
  received_at?: string;
  payload_sha256?: string;
  idempotency_key?: string;
}

export interface ImplementationOverlapProjection {
  target_a_id: string;
  target_b_id: string;
  resolution_a_id: string;
  resolution_b_id: string;
  severity: string;
  reason_code: string;
  relative_path: string | null;
  qualified_symbol: string | null;
  acknowledgement?: { id: string; disposition: string } | null;
}

export interface CodeTraceabilityProjection {
  subject_type: CodeTraceabilitySubjectType;
  subject_id: string;
  subject_version: number;
  profile: CodeTraceabilityProfile;
  context_scope: 'default' | 'gate';
  evidence: CodeTraceabilityEvidence[];
  inherited_evidence_ids: string[];
  direct_evidence_ids: string[];
  referenced_evidence_ids: string[];
  links: CodeTraceabilityEvidenceLink[];
  dispositions: CodeTraceabilityDisposition[];
  targets: ImplementationTargetProjection[];
  resolutions: ImplementationTargetResolution[];
  executions?: ImplementationTargetExecutionRecordProjection[];
  overlaps: ImplementationOverlapProjection[];
  waivers: CodeTraceabilityWaiver[];
  heads: Array<{
    source_ref: string;
    generation: number;
    current_receipt_id: string | null;
    state: string;
  }>;
  counts: Record<string, number>;
  coverage: {
    total: number;
    linked: number;
    dispositioned: number;
    pending: number;
    pending_ids: string[];
    coverage_pct: number;
    skipped?: boolean;
  };
  resolution_freshness: Record<string, {
    state: string;
    currentness: CodeTraceabilityReceiptCurrentness;
    resolution_id: string | null;
    target_revision?: number;
  }>;
  gate_readiness: {
    mode: string;
    allowed: boolean;
    passed: boolean;
    evidence_coverage_skipped?: boolean;
    blockers: Array<{ code: string; message: string; blocking: boolean }>;
    receipt_currentness: Record<string, CodeTraceabilityReceiptCurrentness>;
    resolution_freshness: Record<string, {
      state: string;
      currentness: CodeTraceabilityReceiptCurrentness;
      resolution_id: string | null;
      target_revision?: number;
    }>;
  };
}

export interface BoardSettings {
  max_scenarios_per_card: number;
  skip_test_coverage_global: boolean;
  skip_rules_coverage_global: boolean;
  skip_trs_coverage_global: boolean;
  /** Missing on legacy board snapshots; effective default is false. */
  skip_code_evidence_coverage_global?: boolean;
  skip_contract_coverage_global: boolean;
  skip_ir_coverage_global: boolean;
  skip_or_coverage_global: boolean;
  skip_task_requirement_link_gate_global?: boolean;
  skip_decisions_coverage_global: boolean;
  skip_cognitive_consolidation?: boolean;
  allow_agent_self_answering?: boolean;
  require_full_context_for_critical_actions?: boolean;
  qa_require_role_separation?: boolean;
  /**
   * Independent-review policy for task validation and sprint evaluation.
   * Missing legacy values resolve to `off`; newly created boards/templates
   * materialize `enforce` in the backend.
   */
  reviewer_separation_mode?: ReviewerSeparationMode;
  require_task_validation: boolean;
  min_confidence: number;
  min_completeness: number;
  max_drift: number;
  // Spec Validation Gate settings (default enabled unless explicitly disabled)
  require_spec_validation?: boolean;
  min_spec_confidence?: number;
  min_spec_clarity?: number;
  min_spec_decidability?: number;
  /** Legacy setting retained for reading older board snapshots. */
  min_spec_completeness?: number;
  min_spec_assertiveness?: number;
  max_spec_ambiguity?: number;
  // Max ambiguity gate for ideation completion (spec 2485780b) — opt-in, default off.
  // Blocks evaluating→done when ideation ambiguity is missing or exceeds the threshold.
  require_ideation_ambiguity_gate?: boolean;
  max_ideation_ambiguity?: number; // 1-5, default 3
  // Ambiguity gate for refinement approval/derivation — opt-in, default off.
  require_refinement_ambiguity_gate?: boolean;
  max_refinement_ambiguity?: number; // 1-5, default 3
  // Resource Gate Level 2 - effective spec resources must be copied/attached to tasks.
  require_spec_resource_task_coverage?: boolean;
  // Spec resource automation - copies selected Spec resources to newly-created/linked cards.
  auto_derive_spec_resources_enabled?: boolean;
  auto_derive_spec_resource_types?: SpecResourceAutoDeriveType[];
  // Design System mockup gate (spec 3a006f65). `blocking` means mockup
  // submissions must carry real Design System identity + evidence.
  design_system_gate_mode?: 'off' | 'advisory' | 'blocking';
  // NC-9 evidence gate bypass (Wave 2 spec 873e98cc, frontend spec 5cb09dbc)
  skip_test_evidence_global?: boolean;
  // Impact-evidence enforcement on execution reports (SK-B2-S1 FR-5).
  // off = no effect; advisory = gated moves succeed but a missing block is
  // recorded in the activity log; require = gated moves reject a conclusion
  // without a minimally populated block.
  impact_evidence_mode?: 'off' | 'advisory' | 'require';
  // Requirement-lint language profiles exposed during preflight to an
  // external evaluating agent. Community persists the configuration and the
  // returned evidence; it does not perform the analysis. Empty/absent means
  // the agent receives only neutral signals (numbers, comparators and units).
  lint_languages?: LintLanguageCode[];
  /** Source-blind policy for observations submitted by authenticated agents. */
  code_traceability?: CodeTraceabilitySettings | null;
  /** Board-scoped analytics policy. Missing legacy values resolve to backend defaults. */
  analytics?: {
    version: 1;
    flow_health: {
      version: 1;
      general_stale_hours: number;
      rejected_stale_hours: number;
      overrides: Record<string, unknown>;
    };
  } | null;
}

export type LintLanguageCode = 'pt-BR' | 'en-US' | 'es-ES' | 'de-DE' | 'fr-FR';

// Spec Validation Gate
export type SpecValidationMetric =
  | 'confidence'
  | 'clarity'
  | 'assertiveness'
  | 'decidability'
  | 'ambiguity';

export interface SpecValidationPinpoint {
  metric: SpecValidationMetric;
  anchor_type: QualityFindingAnchorType;
  anchor_ref?: string | null;
  detail: string;
}

export interface SpecValidation {
  id: string;
  validation_id?: string;
  validation_edition?: number;
  is_current?: boolean;
  spec_id: string;
  board_id: string;
  reviewer_id: string;
  reviewer_name?: string | null;
  /** Canonical human validation fields. */
  score?: number | null;
  summary?: string | null;
  confidence?: number | null;
  confidence_justification?: string | null;
  clarity?: number | null;
  clarity_justification?: string | null;
  decidability?: number | null;
  decidability_justification?: string | null;
  pinpoints?: SpecValidationPinpoint[] | null;
  /** Legacy dimension fields remain readable in immutable history. */
  completeness?: number | null;
  completeness_justification?: string | null;
  assertiveness?: number | null;
  assertiveness_justification?: string | null;
  ambiguity?: number | null;
  ambiguity_justification?: string | null;
  general_justification?: string | null;
  recommendation?: 'approve' | 'reject' | null;
  outcome?: 'success' | 'failed' | null;
  threshold_violations?: string[] | null;
  resolved_thresholds?: {
    min_spec_confidence?: number;
    min_spec_clarity?: number;
    min_spec_assertiveness?: number;
    min_spec_decidability?: number;
    max_spec_ambiguity?: number;
    /** Legacy threshold retained for old immutable records. */
    min_spec_completeness?: number;
  } | null;
  created_at: string;
  spec_status?: string | null;
  active?: boolean | null;
  /** Null marks legacy evidence that is available only in previous results. */
  edition?: number | null;
  lifecycle_state?: 'current' | 'previous' | 'history_only' | null;
}

export interface SpecValidationSubmitPayload {
  expected_validation_edition: number;
  expected_spec_version: number;
  expected_head_revision: number;
  confidence: number;
  confidence_justification: string;
  clarity: number;
  clarity_justification: string;
  assertiveness: number;
  assertiveness_justification: string;
  decidability: number;
  decidability_justification: string;
  ambiguity: number;
  ambiguity_justification: string;
  pinpoints: SpecValidationPinpoint[];
  recommendation: 'approve' | 'reject';
}

export interface SpecValidationSubmitResponse {
  validation_id: string;
  validation_edition: number;
  is_current: boolean;
  spec_status?: string | null;
  outcome?: 'success' | 'failed' | null;
  threshold_violations?: string[] | null;
}

export interface SpecValidationList {
  spec_id: string;
  current_validation_id: string | null;
  validations: SpecValidation[];
  total?: number;
  limit?: number;
  offset?: number;
  has_more?: boolean;
}

/** Bounded human projection for the current Spec validation edition. */
export interface SpecValidationCurrentSummary {
  spec_id: string;
  edition: number;
  lifecycle_state: 'pending' | 'current';
  current_validation: SpecValidation | null;
  previous_count: number;
}

// Curated Spec checklist (/specify/v1)
export type ChecklistMode = 'off' | 'advisory' | 'blocking';
export type ChecklistOutcome = 'pass' | 'fail' | 'not_applicable';
export type ChecklistStateStatus =
  | 'off'
  | 'not_started'
  | 'current'
  | 'stale'
  | 'failed';

export interface ChecklistTemplateItem {
  item_id: string;
  title_en: string;
  title_pt: string;
  description_en: string;
  description_pt: string;
  allow_na: boolean;
}

export interface ChecklistTemplate {
  template_id: string;
  version: '/specify/v1';
  digest: string;
  items: ChecklistTemplateItem[];
}

export interface ChecklistBinding {
  id: string;
  board_id: string;
  target_type: 'spec';
  phase: 'spec_validation';
  mode: ChecklistMode;
  version: number;
  expected_revision: number;
  digest: string;
  template_version_id: '/specify/v1';
}

export interface ChecklistBindingUpdateResult {
  binding_id: string;
  revision: number;
  effective: ChecklistBinding;
}

export interface ChecklistItemResult {
  item_id: string;
  outcome: ChecklistOutcome;
  anchor: string;
  rationale: string | null;
}

export interface ChecklistReceipt {
  id: string;
  board_id: string;
  spec_id: string;
  spec_version: number;
  spec_edition?: number | null;
  content_digest: string;
  input_digest: string;
  template_version_id: '/specify/v1';
  template_digest: string;
  binding_version: number;
  binding_id: string;
  binding_mode: ChecklistMode;
  source: 'native' | 'legacy_unverified';
  request_digest: string;
  head_revision: number;
  predecessor_receipt_id: string | null;
  created_by: string;
  created_at: string;
  outcome: 'pass' | 'fail';
  results: ChecklistItemResult[];
  blocking_satisfied: boolean;
}

export interface ChecklistCurrentness {
  current: boolean;
  stale_reasons: string[];
}

export interface ChecklistGateDecision {
  mode: ChecklistMode;
  allowed: boolean;
  reason: string;
  currentness: ChecklistCurrentness | null;
}

export interface ChecklistSpecState {
  status: ChecklistStateStatus;
  subject: {
    board_id: string;
    spec_id: string;
    spec_version: number;
    spec_edition?: number;
    content_digest: string;
    input_digest: string;
    status: string;
    archived: boolean;
  };
  binding: ChecklistBinding;
  current_receipt: ChecklistReceipt | null;
  currentness: ChecklistCurrentness | null;
  gate: ChecklistGateDecision;
}

export interface ChecklistExecutionStartResult {
  execution_id: string;
  items: ChecklistTemplateItem[];
  subject_digest: string;
  template_digest: string;
  spec_edition?: number;
}

export interface ChecklistExecutionStartRequest {
  spec_edition: number;
  expected_spec_version: number;
  binding_version: number;
}

export interface ChecklistExecutionSubmitRequest {
  spec_edition: number;
  expected_spec_version: number;
  execution_id: string;
  item_results: ChecklistItemResult[];
}

export interface ChecklistExecutionSubmitResult {
  receipt_id: string;
  outcome: 'pass' | 'fail';
  head_revision: number;
}

export interface ChecklistReceiptView {
  receipt: ChecklistReceipt;
  is_head: boolean;
  currentness: ChecklistCurrentness;
  gate: ChecklistGateDecision;
}

export interface ChecklistReceiptPage {
  items: ChecklistReceiptView[];
  total_filtered: number;
  total_overall: number;
  offset: number;
  limit: 25 | 50 | 100;
}

// Guideline types
export type GuidelineScope = 'global' | 'inline';

export interface Guideline {
  id: string;
  title: string;
  content: string;
  tags: string[] | null;
  scope: GuidelineScope;
  board_id: string | null;
  owner_id: string;
  version?: number;
  semantic_version?: string;
  revision_id?: string;
  revision_digest?: string;
  context_scope?: 'all';
  created_at: string;
  updated_at: string;
}

export interface BoardGuidelineEntry {
  id: string;
  guideline: Guideline;
  priority: number;
  scope: GuidelineScope;
  binding_id?: string;
  binding_revision?: number;
  /**
   * Exact policy-binding fields. Optional only for backwards-compatible
   * deserialization; policy mutation UIs must fail closed when they are absent.
   */
  enforcement?: 'advisory' | 'blocking';
  minimum_confidence?: number;
  metric_threshold_overrides?: Record<string, number>;
  binding_state?: 'active' | 'unlinked';
  source_kind?: 'native' | 'default_materialization';
}

export interface Board {
  id: string;
  name: string;
  description: string | null;
  owner_id: string;
  settings: BoardSettings | null;
  created_at: string;
  updated_at: string;
  cards: Card[];
  agents: AgentSummary[];
}

// Board summary (without nested items)
export interface BoardSummary {
  id: string;
  name: string;
  description: string | null;
  owner_id: string;
  settings: BoardSettings | null;
  created_at: string;
  updated_at: string;
}

// API request types
export interface CreateBoardRequest {
  name: string;
  description?: string;
}

export interface UpdateBoardRequest {
  name?: string;
  description?: string;
  settings?: Partial<BoardSettings>;
}

export interface CreateCardRequest {
  title: string;
  description?: string;
  details?: string;
  status?: CardStatus;
  priority?: CardPriority;
  assignee_id?: string;
  due_date?: string;
  labels?: string[];
  spec_id?: string;
  // Bug card fields
  card_type?: CardType;
  origin_task_id?: string;
  severity?: BugSeverity;
  expected_behavior?: string;
  observed_behavior?: string;
  steps_to_reproduce?: string;
  action_plan?: string;
  knowledge_propagation?: KnowledgePropagationEnvelopeV2;
}

export interface UpdateCardRequest {
  title?: string;
  description?: string;
  details?: string;
  status?: CardStatus;
  priority?: CardPriority;
  position?: number;
  assignee_id?: string;
  due_date?: string;
  labels?: string[];
  sprint_id?: string | null;
  test_scenario_ids?: string[];
  screen_mockups?: ScreenMockup[];
  knowledge_bases?: CardKnowledgeBase[];
  // Bug card fields
  severity?: BugSeverity;
  expected_behavior?: string;
  observed_behavior?: string;
  steps_to_reproduce?: string;
  action_plan?: string;
  linked_test_task_ids?: string[];
  skip_task_requirement_link_gate?: boolean;
}

// SK-B2-S1 — declared impact evidence (schema v1). A CLAIM, not authority:
// validators keep diffing declared vs real. Mirrors the closed core shape.
export type ImpactEvidenceRepo = 'core' | 'community';
export type ImpactEvidenceChangeKind =
  | 'created'
  | 'modified'
  | 'deleted'
  | 'renamed';
export type ImpactEvidenceSymbolKind =
  | 'function'
  | 'class'
  | 'method'
  | 'component'
  | 'port'
  | 'other';
export type ImpactEvidenceSymbolAction = 'created' | 'modified' | 'deleted';
export type ImpactEvidenceSurfaceKind =
  | 'rest_route'
  | 'mcp_tool'
  | 'mcp_resource'
  | 'ui_component'
  | 'table'
  | 'cli_command'
  | 'event'
  | 'migration'
  | 'other';
export type ImpactEvidenceTestAction = 'added' | 'updated';

export interface ImpactEvidenceFile {
  repo: ImpactEvidenceRepo;
  path: string;
  change_kind: ImpactEvidenceChangeKind;
  previous_path?: string | null;
  note?: string | null;
}

export interface ImpactEvidenceSymbol {
  name: string;
  kind: ImpactEvidenceSymbolKind;
  action: ImpactEvidenceSymbolAction;
  repo: ImpactEvidenceRepo;
  file: string;
}

export interface ImpactEvidenceSurface {
  kind: ImpactEvidenceSurfaceKind;
  identifier: string;
}

export interface ImpactEvidenceTest {
  action: ImpactEvidenceTestAction;
  repo: ImpactEvidenceRepo;
  test_file_path: string;
  test_function?: string | null;
  scenario_id?: string | null;
}

export interface ImpactEvidence {
  schema_version: 1;
  files: ImpactEvidenceFile[];
  symbols: ImpactEvidenceSymbol[];
  surfaces: ImpactEvidenceSurface[];
  tests: ImpactEvidenceTest[];
  evidence_refs: string[];
}

export interface ConclusionEntry {
  text: string;
  author_id: string;
  created_at: string;
  completeness: number;
  completeness_justification: string;
  drift: number;
  drift_justification: string;
  source?: 'move_to_validation' | 'move_to_done' | 'task_validation' | string;
  validation_id?: string;
  impact_evidence?: ImpactEvidence | null;
}

export interface MoveCardRequest {
  status: CardStatus;
  position?: number | null;
  before_id?: string | null;
  after_id?: string | null;
  placement?: 'start' | 'end' | null;
  conclusion?: string;
  completeness?: number;
  completeness_justification?: string;
  drift?: number;
  drift_justification?: string;
  /** Required when status === 'cancelled'; ignored otherwise. */
  cancellation_reason?: string;
  /** Optional declared impact block; omit entirely when no rows (AC-10). */
  impact_evidence?: ImpactEvidence;
}

export type BugWorkflowRemediationPath =
  | 'path_a_reuse_existing_scenario'
  | 'path_b_semantic_gap'
  | 'path_c_hotfix_lane'
  | 'standard_sprint'
  | 'none';

export type BugWorkflowNextAction =
  | 'create_regression_test_card'
  | 'escalate_semantic_gap'
  | 'assign_hotfix_lane'
  | 'activate_hotfix_lane'
  | 'assign_sprint'
  | 'activate_sprint'
  | 'none';

export type BugWorkflowHotfixLaneStatus =
  | 'not_applicable'
  | 'missing'
  | 'inactive'
  | 'ready';

export interface BugWorkflowRemediationAction {
  action_id: BugWorkflowNextAction | string;
  label: string;
  description: string;
  primary: boolean;
}

export interface BugWorkflowRemediationMessage {
  reason_code: string;
  remediation_path: BugWorkflowRemediationPath;
  next_action: BugWorkflowNextAction;
  semantic_gap_required: boolean;
  eligible_scenarios_count: number;
  hotfix_lane_status: BugWorkflowHotfixLaneStatus;
  message: string;
  detail: string;
  actions: BugWorkflowRemediationAction[];
  facts: Record<string, unknown>;
}

export interface BugRegressionScenarioCandidate {
  scenario_id: string;
  title?: string | null;
  reason: string;
  source_task_id?: string;
  detail?: string | null;
}

export interface BugRegressionScenarioPreview {
  bug_id: string;
  spec_id: string;
  origin_task_id?: string | null;
  affected_task_ids: string[];
  eligible_scenarios: BugRegressionScenarioCandidate[];
  rejected_scenarios: BugRegressionScenarioCandidate[];
  next_action: BugWorkflowNextAction;
  semantic_gap_required: boolean;
  spec_mutation_required: boolean;
  remediation: BugWorkflowRemediationMessage;
}

// ==================== PATH B AMENDMENT REVISIONS (spec be089cd3) ====================

export interface AmendmentRevisionEligibility {
  lineage_eligible: boolean;
  canonicalization_candidate: boolean;
  blocked: boolean;
  reason_code: string;
}

export interface AmendmentRevision {
  id: string;
  board_id: string;
  original_spec_id: string;
  origin_bug_id: string;
  revision_spec_id?: string | null;
  status: string; // draft | review | approved | done | cancelled | superseded
  lineage_state: string; // incomplete | complete
  origin_task_ids: string[];
  affected_task_ids: string[];
  regression_scenario_ids: string[];
  regression_test_task_ids: string[];
  automated_regression_refs: string[];
  eligibility: AmendmentRevisionEligibility;
  created_at?: string | null;
  updated_at?: string | null;
}

// coverage_state is rendered tolerant to BOTH vocabularies seen in the system:
//   not_applicable | coverage_pending | path_b_ready  (resolver)
//   missing | pending | validated                      (spec prose)
// pending/coverage_pending must NEVER look closure-ready.
export type AmendmentCoverageState =
  | 'not_applicable'
  | 'coverage_pending'
  | 'path_b_ready'
  | 'missing'
  | 'pending'
  | 'validated'
  | string;

export interface AmendmentPathBResolution {
  available?: boolean;
  coverage_state?: AmendmentCoverageState | null;
  coverage_pending_scenarios?: string[] | null;
  missing_links?: string[] | null;
  safe_next_actions?: string[] | null;
  next_action?: string | null;
  eligible_regression_artifacts?: string[] | null;
  rejected_regression_artifacts?: string[] | null;
  rejected_scenarios?: BugRegressionScenarioCandidate[] | null;
  amendment_revision_id?: string | null;
  // structured error fields when available === false
  error?: string | null;
  code?: string | null;
  message?: string | null;
}

export interface AmendmentRevisionListResponse {
  board_id: string;
  bug_id: string;
  original_spec_id?: string | null;
  revisions: AmendmentRevision[];
  path_b_resolution: AmendmentPathBResolution;
}

export interface CreateAmendmentRevisionRequest {
  original_spec_id?: string;
  revision_spec_id?: string;
  origin_task_ids?: string[];
  affected_task_ids?: string[];
  regression_scenario_ids?: string[];
  regression_test_task_ids?: string[];
  automated_regression_refs?: string[];
}

export interface AssociateAmendmentArtifactsRequest {
  regression_scenario_ids?: string[];
  regression_test_task_ids?: string[];
  automated_regression_refs?: string[];
}

// ==================== DEFAULT BOARD CONFIGURATION (spec 9df814bc) ============

export interface DefaultGuidelineRevisionPin {
  revision_id: string;
  revision_number: number;
  semantic_version: string;
  revision_digest: string;
}

export interface DefaultBoardConfigGuidelineRef
  extends DefaultGuidelineRevisionPin {
  guideline_id: string;
  priority: number;
}

export interface DefaultBoardConfigDesignSystemRef {
  design_system_id?: string;
  version?: number | null;
  gate_mode?: 'off' | 'advisory' | 'blocking' | null;
  snapshot?: Record<string, unknown> | null;
}

export interface SetDefaultDesignSystemRequest {
  design_system_id: string;
  version?: number | null;
  gate_mode?: 'off' | 'advisory' | 'blocking';
  snapshot?: Record<string, unknown> | null;
}

export interface DefaultBoardConfigTemplate {
  id: string;
  version: number;
  status: string;
  is_active: boolean;
  scope: string;
  settings_payload: Record<string, unknown>;
  guideline_default_refs: DefaultBoardConfigGuidelineRef[];
  design_system_default_ref: DefaultBoardConfigDesignSystemRef | null;
  spec_checklist_mode?: ChecklistMode | null;
  created_by: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface DefaultBoardConfigActiveResponse {
  scope: string;
  active: DefaultBoardConfigTemplate | null;
}

export interface DefaultBoardConfigVersionsResponse {
  scope: string;
  active_id: string | null;
  versions: DefaultBoardConfigTemplate[];
}

export interface DefaultBoardConfigDiffField {
  field: string;
  template_value: unknown;
  current_value: unknown;
  state: string;
}

export interface DefaultBoardConfigDiff {
  board_id: string;
  snapshot_state: 'applied' | 'legacy_no_snapshot';
  applied_template_id: string | null;
  applied_template_version: number | null;
  active_template_id: string | null;
  active_template_version: number | null;
  is_outdated: boolean;
  fields: DefaultBoardConfigDiffField[];
}

export interface CreateDefaultBoardConfigVersionRequest {
  settings_payload?: Record<string, unknown> | null;
  scope?: string;
  guideline_default_refs?: DefaultBoardConfigGuidelineRef[] | null;
  design_system_default_ref?: DefaultBoardConfigDesignSystemRef | null;
  spec_checklist_mode?: ChecklistMode;
  activate?: boolean;
}

// Guideline defaults administration (spec 8a2fad91 / card 5cb88511)
export interface DefaultGuidelineCandidate {
  guideline_id: string;
  title: string;
  scope: string;
  guideline_version: number;
  revision_id: string;
  revision_number: number;
  semantic_version: string;
  revision_digest: string;
  head_revision: DefaultGuidelineRevisionPin;
  default_revision: DefaultGuidelineRevisionPin | null;
  retired: boolean;
  eligible: boolean;
  eligibility_reason: 'guideline_retired' | null;
  is_default: boolean;
  priority: number | null;
}

export interface DefaultGuidelineCandidatesResponse {
  scope: string;
  template_id: string | null;
  template_version: number | null;
  candidates: DefaultGuidelineCandidate[];
}

export type * from './policy-governance';

// Design System catalog (spec 3a006f65 / card 1392f59d)
export interface DesignSystem {
  id: string;
  scope: string;
  board_id: string | null;
  title: string;
  /** Present only on detail/full projections. Catalog summary pages omit it. */
  payload?: Record<string, unknown> | null;
  payload_available?: boolean;
  version: number;
  status: string;
  owner_id: string;
  created_at: string | null;
  updated_at: string | null;
  profile?: 'summary' | 'detail' | 'full' | 'legacy';
}

export interface DesignSystemListPage {
  items: DesignSystem[];
  count: number;
  next_cursor: string | null;
  profile: 'summary';
}

export interface BoardDesignSystemEffective {
  source: string;
  design_system_id: string | null;
  version: number | null;
  title?: string | null;
  status?: string | null;
  scope?: string | null;
  gate_mode?: string | null;
  exists?: boolean;
  configured?: boolean;
  resolvable?: boolean;
  mandate?: boolean;
}

export interface BoardDesignSystemEffectiveResponse {
  board_id: string;
  effective: BoardDesignSystemEffective | null;
  configured?: boolean;
  resolvable?: boolean;
  mandate?: boolean;
  gate_mode?: string;
}

export interface CreateDesignSystemRequest {
  title: string;
  scope?: string;
  board_id?: string | null;
  payload?: Record<string, unknown> | null;
  status?: string;
}

export interface UpdateDesignSystemRequest {
  title?: string;
  payload?: Record<string, unknown> | null;
  status?: string;
}

export interface CreateAgentRequest {
  name: string;
  description?: string;
  objective?: string;
  permissions?: string[];
  preset_id?: string;
  permission_flags?: PermissionFlags;
}

export interface UpdateAgentRequest {
  name?: string;
  description?: string;
  objective?: string;
  is_active?: boolean;
  permissions?: string[];
  preset_id?: string | null;
  permission_flags?: Record<string, unknown> | null;
}

export interface CreateQARequest {
  question: string;
}

export interface AnswerQARequest {
  answer: string;
}

export interface CreateCommentRequest {
  content: string;
  comment_type?: 'text' | 'choice' | 'multi_choice';
  choices?: ChoiceOption[];
  allow_free_text?: boolean;
}

export interface RespondToChoiceRequest {
  selected: string[];
  free_text?: string;
}

export interface UpdateCommentRequest {
  content: string;
}

// Board share
export interface BoardShare {
  id: string;
  board_id: string;
  user_id: string;
  realm_id: string;
  permission: 'viewer' | 'editor' | 'admin';
  shared_by: string;
  created_at: string;
}

export interface ShareBoardRequest {
  user_id: string;
  permission: 'viewer' | 'editor' | 'admin';
}

export interface UpdateShareRequest {
  permission: 'viewer' | 'editor' | 'admin';
}

// Spec request types
export interface CreateSpecRequest {
  title: string;
  description?: string;
  context?: string;
  functional_requirements?: string[];
  technical_requirements?: string[];
  acceptance_criteria?: string[];
  test_scenarios?: TestScenarioWrite[];
  decisions?: Decision[];
  integration_requirements?: IntegrationRequirement[];
  observability_requirements?: ObservabilityRequirement[];
  status?: SpecStatus;
  assignee_id?: string;
  labels?: string[];
  ideation_id?: string;
  refinement_id?: string;
}

export interface UpdateSpecRequest extends TaskValidationGateOverride {
  title?: string;
  description?: string;
  context?: string;
  functional_requirements?: string[];
  technical_requirements?: string[];
  acceptance_criteria?: string[];
  test_scenarios?: TestScenarioWrite[];
  business_rules?: BusinessRule[];
  api_contracts?: ApiContract[];
  integration_requirements?: IntegrationRequirement[];
  observability_requirements?: ObservabilityRequirement[];
  decisions?: Decision[];
  screen_mockups?: ScreenMockup[];
  skip_test_coverage?: boolean;
  skip_code_evidence_coverage?: boolean;
  skip_contract_coverage?: boolean;
  skip_ir_coverage?: boolean;
  skip_or_coverage?: boolean;
  skip_decisions_coverage?: boolean;
  skip_qualitative_validation?: boolean;
  validation_threshold?: number;
  assignee_id?: string;
  labels?: string[];
}

export interface MoveSpecRequest {
  status: SpecStatus;
  /** Required when status === 'cancelled'; ignored otherwise. */
  cancellation_reason?: string;
}

// Story request types
export interface CreateTopicRequest {
  name: string;
  description?: string;
}

export interface UpdateTopicRequest {
  name?: string;
  description?: string | null;
  archived?: boolean;
}

export interface CreateStoryRequest {
  title: string;
  description: string;
  topic_id: string;
  actor?: string;
  goal?: string;
  benefit?: string;
  labels?: string[];
  status?: StoryStatus;
  assignee_id?: string;
  screen_mockups?: ScreenMockup[];
}

export interface UpdateStoryRequest {
  title?: string;
  description?: string;
  topic_id?: string;
  actor?: string | null;
  goal?: string | null;
  benefit?: string | null;
  labels?: string[];
  assignee_id?: string | null;
  screen_mockups?: ScreenMockup[];
}

export interface MoveStoryRequest {
  status: StoryStatus;
}

export interface StoryConversionRequest {
  story_ids: string[];
  ideation_id?: string;
  title?: string;
  description?: string;
  problem_statement?: string;
  proposed_approach?: string;
  mockup_ids?: string[];
  mark_converted?: boolean;
}

export interface StoryConversionResponse {
  success: boolean;
  ideation: Record<string, unknown>;
  links: StoryIdeationLink[];
  propagated_mockups: number;
}

// Ideation request types
export interface CreateIdeationRequest {
  title: string;
  description?: string;
  problem_statement?: string;
  proposed_approach?: string;
  assignee_id?: string;
  labels?: string[];
}

export interface UpdateIdeationRequest {
  title?: string;
  description?: string;
  problem_statement?: string;
  proposed_approach?: string;
  screen_mockups?: ScreenMockup[];
  assignee_id?: string;
  labels?: string[];
}

// Refinement request types
export interface CreateRefinementRequest {
  ideation_id: string;
  title: string;
  description?: string;
  in_scope?: string[];
  out_of_scope?: string[];
  analysis?: string;
  decisions?: string[];
  assignee_id?: string;
  labels?: string[];
}

export interface UpdateRefinementRequest {
  title?: string;
  description?: string;
  in_scope?: string[];
  out_of_scope?: string[];
  analysis?: string;
  decisions?: string[];
  screen_mockups?: ScreenMockup[];
  assignee_id?: string;
  labels?: string[];
}


// Knowledge request types
export interface CreateIdeationKnowledgeRequest {
  title: string;
  description?: string;
  content: string;
  mime_type?: string;
}

export interface CreateSpecKnowledgeRequest {
  title: string;
  description?: string;
  content: string;
  mime_type?: string;
}

// Column type for UI
export interface KanbanColumn {
  status: CardStatus;
  label: string;
  cards: CardSummary[];
}
