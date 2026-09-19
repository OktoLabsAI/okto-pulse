export type VerificationProfile = 'functional' | 'integration' | 'technical' | 'operational';
export type VerificationRequirementType = 'functional_requirement' | 'technical_requirement' | 'integration_requirement' | 'observability_requirement' | 'business_rule';
export interface VerificationRequirementRef { requirement_type: VerificationRequirementType; requirement_id: string }
export interface VerificationInheritanceSelection {
  source: VerificationRequirementRef;
  source_digest: string;
  criterion_ids: string[];
  covered_aspect: string;
}
export interface RequirementVerification {
  mode: 'explicit' | 'inherited';
  required_profiles: VerificationProfile[];
  inheritance?: VerificationInheritanceSelection[];
  evidence_policy_ref?: 'pulse-verification/v1';
}
export interface RequirementVerificationPath {
  criterion_id: string;
  profile: VerificationProfile;
  path: VerificationRequirementRef[];
  aspects: Array<string | null>;
  source_digests: string[];
  method_plan_complete?: boolean;
  verification_work_complete?: boolean;
  planning_blockers?: string[];
  scenario_count?: number;
  scenarios_truncated?: boolean;
  scenario_plans?: Array<{
    scenario_id: string; method: string | null; method_admitted: boolean;
    method_plan_complete: boolean; blockers: string[]; work_blockers: string[];
    test_card_ids: string[]; test_card_count: number; test_cards_truncated: boolean;
  }>;
}
export interface RequirementVerificationRow extends VerificationRequirementRef {
  contribution_blockers?: string[];
  contribution_count?: number;
  contributions_truncated?: boolean;
  implementation_contributions?: Array<{
    card_id: string; origin: 'direct' | 'inherited'; scope: 'whole_requirement' | 'selected_criteria';
    criterion_ids: string[]; summary: string | null; scope_sha256: string;
    criterion_count: number; criteria_truncated: boolean;
    sources: Array<VerificationRequirementRef & { scope_sha256: string }>;
    source_count: number; sources_truncated: boolean;
  }>;
  title: string;
  source_digest: string | null;
  verification: RequirementVerification | null;
  qualification_origin: 'authored' | 'absent_or_invalid';
  qualification_resolved: boolean;
  default_proposal: { version: string; origin: 'product'; requires_author_acceptance: true; verification: RequirementVerification } | null;
  criteria_paths: RequirementVerificationPath[];
  blockers: Array<{ code: string; profile?: string; criterion_id?: string; source?: VerificationRequirementRef }>;
  blocker_count: number;
  blockers_truncated: boolean;
  paths_total: number;
  paths_offset: number;
  paths_has_more: boolean;
  next_paths_offset: number | null;
  paths_unavailable: boolean;
}
export interface RequirementVerificationResponse {
  contract_version: 'requirement-verification/v1';
  board_id: string; spec_id: string; spec_version: number; spec_edition: number;
  spec_status: string; archived: boolean;
  population_complete: boolean; criteria_resolution_complete: boolean;
  total: number | null; population_total: number | null; resolved_count: number;
  counts_scope: 'complete' | 'observed';
  offset: number; limit: number; has_more: boolean; next_offset: number | null;
  items: RequirementVerificationRow[];
  issues: Array<{ code: string; field?: string; criterion_id?: string }>;
  issue_count: number; issues_truncated: boolean;
  methods_evaluated: boolean; execution_evaluated: false;
  implementation_plan_evaluated?: boolean;
  implementation_scope?: 'qualified_requirements';
  implementation_plan_complete?: boolean;
  implementation_population_complete?: boolean;
  implementation_issues?: string[];
  verification_work_evaluated?: boolean;
  method_plan_complete?: boolean; verification_work_complete?: boolean;
  planning_population_complete?: boolean;
  planning_issues?: Array<{ code: string }>;
  planning_issue_count?: number; planning_issues_truncated?: boolean;
  semantic_review_evaluated: false; delivery_evaluated: false; rollout_evaluated: false;
}
