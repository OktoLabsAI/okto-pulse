/**
 * Type definitions for the Dashboard application
 */

// Card status enum matching backend
export type CardStatus =
  | 'not_started'
  | 'started'
  | 'in_progress'
  | 'on_hold'
  | 'done'
  | 'cancelled';

export const CARD_STATUSES: CardStatus[] = [
  'not_started',
  'started',
  'in_progress',
  'on_hold',
  'done',
  'cancelled',
];

export const STATUS_LABELS: Record<CardStatus, string> = {
  not_started: 'Not Started',
  started: 'Started',
  in_progress: 'In Progress',
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
export type CardType = 'normal' | 'bug';

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
export type SpecStatus = 'draft' | 'review' | 'approved' | 'in_progress' | 'done' | 'cancelled';

export const SPEC_STATUSES: SpecStatus[] = [
  'draft', 'review', 'approved', 'in_progress', 'done', 'cancelled',
];

export const SPEC_STATUS_LABELS: Record<SpecStatus, string> = {
  draft: 'Draft',
  review: 'Review',
  approved: 'Approved',
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
  question_type: 'text' | 'choice' | 'multi_choice';
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
  question_type: 'text' | 'choice' | 'multi_choice';
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
  question_type: 'text' | 'choice' | 'multi_choice';
  choices: SpecQAChoiceOption[] | null;
  allow_free_text: boolean;
  answer: string | null;
  selected: string[] | null;
  asked_by: string;
  answered_by: string | null;
  created_at: string;
  answered_at: string | null;
}

// Spec Skill
export interface SkillSection {
  id: string;
  title: string;
  description: string;
  level: 'summary' | 'detail' | 'full';
  content: string;
}

export interface SpecSkill {
  id: string;
  spec_id: string;
  skill_id: string;
  name: string;
  description: string;
  type: string;
  version: string;
  tags: string[] | null;
  sections: SkillSection[] | null;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface SpecSkillSummary {
  skill_id: string;
  name: string;
  description: string;
  type: string;
  tags: string[] | null;
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
  screen_mockups: ScreenMockup[] | null;
  skip_test_coverage: boolean;
  skip_rules_coverage?: boolean;
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
  skills: SpecSkillSummary[];
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
  conclusions: ConclusionEntry[] | null;
  attachments: Attachment[];
  qa_items: QAItem[];
  comments: Comment[];
  // Bug card fields (optional for backwards compat with existing cards)
  card_type?: CardType;
  origin_task_id?: string | null;
  severity?: BugSeverity | null;
  expected_behavior?: string | null;
  observed_behavior?: string | null;
  steps_to_reproduce?: string | null;
  action_plan?: string | null;
  linked_test_task_ids?: string[] | null;
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
  // Bug card fields (for kanban display — optional for backwards compat)
  card_type?: CardType;
  origin_task_id?: string | null;
  severity?: BugSeverity | null;
  linked_test_task_ids?: string[] | null;
  archived?: boolean;
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
  test_scenario_ids?: string[];
  screen_mockups?: ScreenMockup[];
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
}

export interface UpdateAgentRequest {
  name?: string;
  description?: string;
  objective?: string;
  is_active?: boolean;
  permissions?: string[];
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
  screen_mockups?: ScreenMockup[];
  skip_test_coverage?: boolean;
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


// Spec Skill request types
export interface CreateSpecSkillRequest {
  skill_id: string;
  name: string;
  description: string;
  type?: string;
  version?: string;
  tags?: string[];
  sections?: SkillSection[];
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
