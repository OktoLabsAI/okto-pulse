export interface ArchitectureContractCandidate {
  id: string;
  root_design_id: string;
  interface_id: string;
  source_digest: string;
  name: string | null;
  contract_type: string | null;
  direction: string | null;
  protocol: string | null;
  contract?: Record<string, unknown>;
  adopted_sources: Array<{ design_id: string; revision: number }>;
  signals: string[];
}

export interface ArchitectureCandidatesResponse {
  contract_version: 'architecture-candidates/v1';
  board_id: string;
  spec_id: string;
  spec_version: number;
  spec_edition: number;
  source_complete: boolean;
  population_state: 'complete' | 'unavailable' | 'unresolved';
  total: number | null;
  total_variants: number | null;
  offset: number;
  limit: number;
  has_more: boolean;
  profile: 'summary' | 'detail';
  issue_counts: Record<string, number>;
  issues_truncated: boolean;
  candidates: ArchitectureContractCandidate[];
  issues: Array<{
    code: string;
    design_id: string | null;
    interface_index: number | null;
    candidate_id: string | null;
  }>;
}
