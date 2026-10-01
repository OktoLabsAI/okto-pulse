export interface LineageQuery {
  subject_ref: string; limit?: number; cursor?: string | null; max_depth?: number; timeout_ms?: number;
}
export interface LineagePathStep {
  source_ref: string; target_ref: string; provenance_ref: string;
  relation: 'precedes' | 'derived_from' | 'belongs_to' | 'originates_bug' | 'feeds_ideation' | 'amendment_of' | 'affects' | 'regression_test';
  direction: 'incoming' | 'outgoing';
}
export interface SourceLineageResponse {
  view: 'lineage'; subject_ref: string; authority: 'informational'; data_source: 'relational';
  scope: { max_depth: number; path_selection: 'one_shortest_path_per_target';
    interpretation: 'workflow_origins_dependencies_and_amendments_not_execution_or_delivery' };
  projection_freshness: { state: 'unknown' | 'incomplete' | 'unavailable'; graph_generation: string | null;
    source_checkpoint: string; projection_checkpoint: null; checked_at: string };
  completeness: { complete_for_scope: boolean; truncated: boolean; limitations: string[] };
  counts: { source_nodes: number; source_relations: number; reached_targets: number };
  frontier_refs: string[];
  items: { subject_ref: string; entity_type: string; title: string; status: string; depth: number; path: LineagePathStep[] }[];
  next_cursor: string | null;
}
