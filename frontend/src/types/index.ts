/**
 * Type definitions for the Dashboard application
 */

// Card status enum matching backend
export type CardStatus =
  | 'not_started'
  | 'started'
  | 'in_progress'
  | 'validation'
  | 'on_hold'
  | 'done'
  | 'cancelled';

export const CARD_STATUSES: CardStatus[] = [
  'not_started',
  'started',
  'in_progress',
  'validation',
  'on_hold',
  'done',
  'cancelled',
];

export const STATUS_LABELS: Record<CardStatus, string> = {
  not_started: 'Not Started',
  started: 'Started',
  in_progress: 'In Progress',
  validation: 'Validation',
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

export type AllowedTransitionEntityType = 'story' | 'ideation' | 'refinement' | 'spec' | 'card' | 'sprint';

export interface AllowedTransition {
  to_status: string;
  label: string;
  gate: string;
  blocked_reason?: string | null;
  preconditions?: string[];
  capabilities?: string[];
  effects?: string[];
  reason_codes?: string[];
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

export interface Sprint {
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
export type QualityAssessmentStaleReason =
  | 'content_changed'
  | 'clarification_changed'
  | 'ruleset_changed'
  | 'taxonomy_changed'
  | 'policy_changed'
  | 'subject_version_changed';
export type QualityAssessmentReceiptState =
  | 'current'
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
  receipt_id: string;
  subject_version: number;
  currentness: QualityCurrentness;
  score: number;
  scale: QualityScaleSummary;
  head_revision: number;
}

/**
 * Optional on list entities by design:
 * - omitted: the actor cannot read Quality (or the projection was not asked);
 * - {}: the actor may read Quality, but no current heads exist.
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
  state: QualityCurrentness;
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
  head_revision: number;
  currentness: QualityCurrentness;
  stale_reasons: QualityAssessmentStaleReason[];
  gate_preview: QualityGatePreview;
}

export interface QualityAssessmentReceiptDetail {
  receipt: QualityAssessmentReceipt;
  currentness: QualityCurrentness;
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
  qa_id_map: Record<string, string>;
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
  version: number;
  assignee_id: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
  labels: string[] | null;
  archived?: boolean;
  skip_ambiguity_gate?: boolean;
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
export interface Spec {
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
}

export interface RefinementAmbiguityGateSkipReceipt {
  skipped: boolean;
  activity_id: string;
  version: number;
}

// Card
export interface Card {
  id: string;
  board_id: string;
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
  // Cancellation justification (set only while status === 'cancelled')
  cancellation_reason?: string | null;
  cancelled_at?: string | null;
  cancelled_by?: string | null;
}

// Validation entry (from backend validation lifecycle)
export interface ValidationEntry {
  id: string;
  verdict: 'pass' | 'fail';
  confidence: number;
  completeness: number;
  drift: number;
  summary: string | null;
  evaluator_id: string;
  created_at: string;
}

// Card for column view (simplified)
export interface CardSummary {
  id: string;
  open_qa_count?: number;
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

// Permission Preset
export interface PermissionPreset {
  id: string;
  owner_id: string | null;
  name: string;
  description: string | null;
  is_builtin: boolean;
  base_preset_id: string | null;
  flags: Record<string, Record<string, Record<string, boolean>>>;
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
  permission_flags: Record<string, Record<string, Record<string, boolean>>> | null;
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

export interface BoardSettings {
  max_scenarios_per_card: number;
  skip_test_coverage_global: boolean;
  skip_rules_coverage_global: boolean;
  skip_trs_coverage_global: boolean;
  skip_contract_coverage_global: boolean;
  skip_ir_coverage_global: boolean;
  skip_or_coverage_global: boolean;
  skip_task_requirement_link_gate_global?: boolean;
  skip_decisions_coverage_global: boolean;
  skip_cognitive_consolidation?: boolean;
  allow_agent_self_answering?: boolean;
  require_full_context_for_critical_actions?: boolean;
  qa_require_role_separation?: boolean;
  require_task_validation: boolean;
  min_confidence: number;
  min_completeness: number;
  max_drift: number;
  // Spec Validation Gate settings (default enabled unless explicitly disabled)
  require_spec_validation?: boolean;
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
}

// Spec Validation Gate
export interface SpecValidation {
  id: string;
  spec_id: string;
  board_id: string;
  reviewer_id: string;
  reviewer_name?: string | null;
  completeness: number;
  completeness_justification: string;
  assertiveness: number;
  assertiveness_justification: string;
  ambiguity: number;
  ambiguity_justification: string;
  general_justification: string;
  recommendation: 'approve' | 'reject';
  outcome: 'success' | 'failed';
  threshold_violations: string[];
  resolved_thresholds?: {
    min_spec_completeness: number;
    min_spec_assertiveness: number;
    max_spec_ambiguity: number;
  } | null;
  created_at: string;
  spec_status?: string | null;
  active?: boolean | null;
}

export interface SpecValidationSubmitPayload {
  completeness: number;
  completeness_justification: string;
  assertiveness: number;
  assertiveness_justification: string;
  ambiguity: number;
  ambiguity_justification: string;
  general_justification: string;
  recommendation: 'approve' | 'reject';
}

export interface SpecValidationList {
  spec_id: string;
  current_validation_id: string | null;
  validations: SpecValidation[];
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
  created_at: string;
  updated_at: string;
}

export interface BoardGuidelineEntry {
  id: string;
  guideline: Guideline;
  priority: number;
  scope: GuidelineScope;
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

export interface DefaultBoardConfigGuidelineRef {
  guideline_id: string;
  priority?: number;
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
  guideline_version: number | null;
  eligible: boolean;
  is_default: boolean;
  priority: number | null;
}

export interface DefaultGuidelineCandidatesResponse {
  scope: string;
  template_id: string | null;
  template_version: number | null;
  candidates: DefaultGuidelineCandidate[];
}

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
  permission_flags?: Record<string, Record<string, Record<string, boolean>>>;
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

export interface UpdateSpecRequest {
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
