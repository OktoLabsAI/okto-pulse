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
