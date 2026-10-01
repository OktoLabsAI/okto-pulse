export type BugClusterGrouping = 'proxy' | 'spec' | 'learning' | 'severity';
export type ClusterFreshness = 'current' | 'lagging' | 'incomplete' | 'unavailable' | 'unknown';
export interface BugClustersQuery {
  from: string;
  to: string;
  group_by: BugClusterGrouping;
  status?: string;
  severity?: string;
  limit?: number;
  cursor?: string | null;
}
export interface BugClusterItem {
  target_ref: string | null;
  title: string;
  validity: 'source' | 'current' | 'previous' | 'unknown';
  assertion_basis: 'origin_proxy' | 'recorded_learning' | 'origin_spec' | 'source_severity';
  causal_conclusion: 'not_established';
  distinct_bug_count: number | null;
  observed_bug_count: number;
  observed_done_count: number;
  observed_resolution_timestamp_count: number;
  observed_median_resolution_hours: number | null;
  bug_refs: string[];
  provenance_refs: string[];
  projection_freshness: ClusterFreshness | 'not_applicable';
}
export interface BugClustersResponse {
  view: 'bugs';
  subject_ref: string;
  group_by: BugClusterGrouping;
  window: { from: string; to: string };
  authority: 'informational';
  data_source: 'relational' | 'mixed_relational_graph';
  projection_freshness: {
    state: ClusterFreshness;
    graph_generation: string | null;
    source_checkpoint: string | null;
    projection_checkpoint: string | null;
    checked_at: string;
  };
  completeness: { complete_for_scope: boolean; truncated: boolean; limitations: string[] };
  distinct_bug_count: number | null;
  observed_bug_count: number;
  cluster_count: number | null;
  observed_cluster_count: number;
  resolution_semantics: 'latest_verified_done_transition_for_current_done_bugs; missing_timestamp_is_unknown';
  items: BugClusterItem[];
  next_cursor: string | null;
}
