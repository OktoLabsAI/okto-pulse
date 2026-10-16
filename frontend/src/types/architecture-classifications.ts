import type { IntegrationRequirementType } from '@/types';

export interface AuthoredArchitectureIR {
  title: string;
  integration_type: IntegrationRequirementType;
  description?: string;
  provider?: string;
  consumer?: string;
  contract_ref?: string;
  endpoint?: string;
  method?: string;
  data_contract?: Record<string, unknown>;
  notes?: string;
}
export interface ArchitectureClassificationDecision {
  candidate_ref: string;
  expected_source_digest: string;
  disposition: 'promote_to_ir' | 'associate_existing_ir' | 'context_only';
  scope_paths: string[];
  integration_requirements?: AuthoredArchitectureIR[];
  integration_requirement_refs?: string[];
  reason?: string;
  remainder_reason?: string;
}
export interface ArchitectureClassificationBatch {
  expected_spec_version: number;
  expected_spec_edition: number;
  idempotency_key: string;
  decisions: ArchitectureClassificationDecision[];
}
export interface ArchitectureClassificationReceipt {
  contract_version: 'architecture-classification/v1';
  board_id: string;
  spec_id: string;
  spec_edition: number;
  spec_version: number;
  idempotency_key: string;
  created_ir_ids: string[];
  decisions: { decision_id: string; candidate_ref: string; source_digest: string; disposition: ArchitectureClassificationDecision['disposition']; integration_requirement_refs: string[]; scope_paths: string[] }[];
  pending_checks: string[];
  replayed: boolean;
}

export type ArchitectureReviewState = 'pending' | 'current' | 'review_required' | 'unresolved' | 'retired' | 'unavailable';

export interface ArchitectureClassificationReviewItem {
  candidate_id: string;
  root_design_id: string;
  interface_id: string;
  name: string | null;
  state: ArchitectureReviewState;
  current_source_digest: string | null;
  analyzed_source_digest: string | null;
  source_variant_count: number;
  source_digests: string[];
  source_digests_truncated: boolean;
  decision_count: number | null;
  dispositions: string[];
  issues: string[];
  remainder_state: 'current' | 'review_required' | null;
  current_contract?: Record<string, unknown> | null;
  analyzed_contract?: Record<string, unknown> | null;
  promotion_suggestion?: {
    scope_paths: ['']; proposed_ir: Partial<AuthoredArchitectureIR>;
    requires_author_review: true; missing_required_fields: string[];
  } | null;
  changed_paths?: string[];
  changed_paths_truncated?: boolean;
  decisions?: {
    decision_id: string;
    spec_version: number;
    actor_id: string;
    classified_at: string;
    disposition: 'promote_to_ir' | 'associate_existing_ir' | 'context_only';
    integration_requirement_refs: string[];
    scope_paths: string[];
    reason: string | null;
    remainder_reason: string | null;
    state: ArchitectureReviewState;
    adopted_sources: { design_id: string; revision: number }[];
  }[];
}

export interface ArchitectureClassificationsResponse {
  contract_version: 'architecture-classification-review/v1';
  board_id: string;
  spec_id: string;
  spec_edition: number;
  spec_version: number;
  source_complete: boolean;
  enumeration_complete: boolean;
  classification_complete: boolean;
  admission_evaluated: false;
  semantic_review_evaluated: false;
  rollout_evaluated: false;
  observed_total: number;
  total: number | null;
  state_counts: Record<ArchitectureReviewState, number>;
  counts_scope: 'complete' | 'observed';
  issues: string[];
  state_filter: ArchitectureReviewState | null;
  profile: 'summary' | 'detail';
  offset: number;
  limit: number;
  has_more: boolean;
  items: ArchitectureClassificationReviewItem[];
}
