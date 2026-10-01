export interface ImpactQuery { limit?: number; cursor?: string | null; max_depth?: number; timeout_ms?: number }
export interface ImpactPathStep {
  source_ref: string; relation: string; target_ref: string; direction: 'incoming' | 'outgoing';
  rule_id: string; layer: string; created_by: string; source_confirmed: boolean; graph_observed: boolean;
}
export interface ImpactItem {
  target_ref: string; target_type: string; title: string; status: string;
  reach: 'direct' | 'indirect'; certainty: 'potential' | 'confirmed_link';
  interpretation: 'potential_shared_card_reach' | 'graph_only_observation' | 'declared_link_not_proven_change';
  path: ImpactPathStep[];
}
export interface DecisionImpactResponse {
  view: 'impact'; subject_ref: string; authority: 'informational'; decision_status: string;
  current_decision: boolean; data_source: 'relational' | 'composed';
  scope: { spec_ref: string; max_depth: number; path_selection: 'one_representative_path_per_target_prefer_confirmed_link' };
  projection_freshness: { state: 'unknown' | 'incomplete' | 'unavailable'; graph_generation: string | null;
    source_checkpoint: string; projection_checkpoint: null; checked_at: string };
  completeness: { complete_for_scope: false; truncated: boolean; limitations: string[] };
  counts: { observed_targets: number; confirmed_link_targets: number; potential_targets: number };
  frontier_refs: string[];
  history: { subject_ref: string; title: string; status: string; supersedes_ref: string | null }[];
  items: ImpactItem[]; next_cursor: string | null;
}
