export interface DecisionVerification {
  obligation_refs: string[];
  inspection?: { condition: string; scope_refs: Array<{ kind: 'spec' | 'obligation'; id: string }> } | null;
}

export interface DecisionReviewSource { reference: string; revision: string; sha256: string }
export type DecisionReviewResult = 'passed' | 'failed' | 'inconclusive' | 'aborted' | 'unavailable';
export interface DecisionReviewEntry {
  decision_id: string;
  expected_scope_sha256: string;
  observed: string;
  result: DecisionReviewResult;
  sources: DecisionReviewSource[];
  reconciles: string[];
}
export interface DecisionReviewInput {
  expected_version: number;
  expected_edition: number;
  expected_review_revision: string;
  idempotency_key: string;
  entries: DecisionReviewEntry[];
}
export interface DecisionReviewProjection {
  board_id: string; spec_id: string; edition: number; version: number;
  review_revision: string; complete: boolean;
  observation_authority: 'authenticated_reviewer_declaration';
  decisions: Array<{
    decision_id: string; status: string; record_ids: string[];
    basis: { scope_sha256: string; sources: DecisionReviewSource[]; expected: string } | null;
    separation: { mode: string; allowed: boolean; warning: boolean; conflicts: string[] } | null;
  }>;
  history: Array<{
    id: string; edition: number; actor_id: string; actor_kind: string; created_at: string; revoked: boolean;
    observations: Array<DecisionReviewEntry & { expected: string; separation: { warning: boolean; conflicts: string[] } }>;
  }>;
}
