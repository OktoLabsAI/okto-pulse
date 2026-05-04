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
  warnings: string[];
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
  created_by: string;
  created_at: string;
  updated_at: string;
  cards: CardSummaryForSpec[];
  qa_items: SprintQAItem[];
}

export interface SprintSummary {
  id: string;
  spec_id: string;
  board_id: string;
  title: string;
  description: string | null;
  status: SprintStatus;
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
  spec_id: string;
  test_scenario_ids?: string[];
  business_rule_ids?: string[];
  start_date?: string;
  end_date?: string;
  labels?: string[];
}

export interface MoveSprintRequest {
  status: SprintStatus;
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
  created_at: string;
}

// Refinement Summary (for nesting in Ideation)
export interface RefinementSummary {
  id: string;
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
  notes: string | null;
}

// Technical Requirement (structured)
export interface TechnicalRequirement {
  id: string;
  text: string;
  linked_task_ids: string[] | null;
}

// Test Scenario
export type TestScenarioType = 'unit' | 'integration' | 'e2e' | 'manual';
export type TestScenarioStatus = 'draft' | 'ready' | 'automated' | 'passed' | 'failed';

export interface TestScenarioEvidence {
  test_file_path?: string | null;
  test_function?: string | null;
  last_run_at?: string | null;
  test_run_id?: string | null;
  output_snippet?: string | null;
}

export interface TestScenario {
  id: string;
  title: string;
  linked_criteria: string[] | null;
  scenario_type: TestScenarioType;
  given: string;
  when: string;
  then: string;
  notes: string | null;
  status: TestScenarioStatus;
  linked_task_ids: string[] | null;
  created_at?: string;
  evidence?: TestScenarioEvidence | null;
}

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
> & Partial<Pick<ArchitectureDesign, 'source_ref' | 'source_version' | 'source_design_id'>>;

export type UpdateArchitectureDesignRequest = Partial<CreateArchitectureDesignRequest> & {
  change_summary?: string;
};

export interface ArchitectureDesignValidationResult {
  valid: boolean;
  issues: string[];
  warnings: string[];
  suggested_fixes: string[];
  summary: Record<string, unknown>;
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
  decisions: Decision[] | null;
  screen_mockups: ScreenMockup[] | null;
  architecture_designs?: ArchitectureDesignSummary[];
  skip_test_coverage: boolean;
  skip_rules_coverage?: boolean;
  skip_decisions_coverage?: boolean;
  skip_contract_coverage?: boolean;
  skip_qualitative_validation?: boolean;
  validation_threshold?: number;
  archived?: boolean;
  pre_archive_status?: string | null;
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
}

// Spec summary (without nested cards)
export interface SpecSummary {
  id: string;
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
  refinements: RefinementSummary[];
  specs: SpecSummary[];
  qa_items: IdeationQAItem[];
}

export interface IdeationSummary {
  id: string;
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
  specs: SpecSummary[];
  qa_items: RefinementQAItem[];
  knowledge_bases: RefinementKnowledgeSummary[];
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
  validations?: ValidationEntry[] | null;
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
  // Bug card fields (for kanban display — optional for backwards compat)
  card_type?: CardType;
  origin_task_id?: string | null;
  severity?: BugSeverity | null;
  linked_test_task_ids?: string[] | null;
  archived?: boolean;
}

// Permission Preset
export interface PermissionPreset {
  id: string;
  name: string;
  description: string | null;
  is_builtin: boolean;
  flags: Record<string, Record<string, Record<string, boolean>>>;
  created_at: string;
}

// Agent (global, always includes api_key)
export interface Agent {
  id: string;
  name: string;
  description: string | null;
  objective: string | null;
  api_key: string;
  is_active: boolean;
  permissions: string[] | null;
  permission_flags: Record<string, Record<string, Record<string, boolean>>> | null;
  preset_id: string | null;
  created_by: string;
  created_at: string;
  last_used_at: string | null;
}

// Agent summary (without sensitive data, used in board context)
export interface AgentSummary {
  id: string;
  name: string;
  description: string | null;
  objective: string | null;
  is_active: boolean;
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
}

// Board
export interface BoardSettings {
  max_scenarios_per_card: number;
  skip_test_coverage_global: boolean;
  skip_rules_coverage_global: boolean;
  skip_trs_coverage_global: boolean;
  skip_contract_coverage_global: boolean;
  skip_decisions_coverage_global: boolean;
  require_task_validation: boolean;
  min_confidence: number;
  min_completeness: number;
  max_drift: number;
  // Spec Validation Gate settings (opt-in, default false)
  require_spec_validation?: boolean;
  min_spec_completeness?: number;
  min_spec_assertiveness?: number;
  max_spec_ambiguity?: number;
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
  position?: number;
  conclusion?: string;
  completeness?: number;
  completeness_justification?: string;
  drift?: number;
  drift_justification?: string;
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
  preset_id?: string;
  permission_flags?: Record<string, Record<string, Record<string, boolean>>>;
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
  decisions?: Decision[];
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
  test_scenarios?: TestScenario[];
  business_rules?: BusinessRule[];
  api_contracts?: ApiContract[];
  decisions?: Decision[];
  screen_mockups?: ScreenMockup[];
  skip_test_coverage?: boolean;
  skip_contract_coverage?: boolean;
  skip_decisions_coverage?: boolean;
  skip_qualitative_validation?: boolean;
  validation_threshold?: number;
  assignee_id?: string;
  labels?: string[];
}

export interface MoveSpecRequest {
  status: SpecStatus;
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


// Spec Knowledge request types
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
