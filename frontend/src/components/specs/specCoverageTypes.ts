export type CoverageSection = 'tests' | 'rules' | 'contracts' | 'trs' | 'decisions' | 'irs' | 'ors';
export type CoverageQuery = { limit?: number; cursor?: string | null; timeout_ms?: number };
export type ProofStatus = 'unknown' | 'proven' | 'partial' | 'missing' | 'satisfied_with_waiver';
export type CoverageItem = {
  kind: 'delivery'; obligation_ref: string; semantic_sha256: string; title: string;
  implementation: ProofStatus; verification: ProofStatus;
  implementation_record_refs: string[]; verification_record_refs: string[];
  implementation_waiver_refs: string[]; verification_waiver_refs: string[];
  required_card_refs: string[]; missing_card_refs: string[]; missing_criteria: string[][];
} | {
  kind: 'structure_node'; node_type: string; subject_ref: string;
  observation: 'observed' | 'not_found_in_projection';
} | {
  kind: 'structure_relation'; source_type: string; source_ref: string; relation: string;
  target_type: string; target_ref: string; rule_id: string; layer: string; created_by: string;
  observation: 'observed_expected' | 'graph_only' | 'not_found_in_projection';
  authority: 'structural_observation_not_delivery_proof';
};
export interface SpecCoverageResponse {
  view: 'coverage'; subject_ref: string; edition: number; authority: 'informational';
  data_source: 'relational' | 'composed';
  projection_freshness: { state: 'unknown' | 'incomplete' | 'unavailable'; graph_generation: string | null;
    source_checkpoint: string; projection_checkpoint: null; checked_at: string };
  completeness: { complete_for_scope: false; truncated: boolean; limitations: string[] };
  structure: { authority: 'spec_coverage_summary'; interpretation: 'planning_links_not_delivery_proof';
    complete_for_scope: boolean; summary: Record<string, number | boolean | string[] | number[]> | null;
    graph: { state: 'observed' | 'restricted' | 'unavailable'; complete_for_scope: false;
      expected_nodes: number | null; observed_nodes: number | null; missing_nodes: number | null;
      missing_relations: number | null; truncated?: boolean; comparison_scope?: string | null; interpretation?: string | null } };
  delivery: { state: 'available' | 'restricted' | 'unavailable'; authority: 'evaluate_delivery_coverage';
    complete_for_scope: boolean; counts: { obligations: number | null; implementation_proven: number | null;
      verification_proven: number | null; observed_obligations: number | null }; blockers: string[];
    rejected_record_refs: string[]; interpretation: string };
  items: CoverageItem[]; next_cursor: string | null;
}
