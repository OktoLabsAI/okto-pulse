import type { SpecStatus } from './index';

/** Public REST vocabulary; Community translates it to the Core domain enums. */
export type SpecDependencyDirection = 'depends_on' | 'required_by';
export type SpecDependencySatisfactionFilter =
  | 'all'
  | 'satisfied'
  | 'unmet';
export type SpecDependencyActiveStateFilter = 'active' | 'removed' | 'all';
export type SpecDependencyLineageFilter =
  | 'all'
  | 'same_ideation'
  | 'cross_ideation';

export interface SpecDependencySpecSummary {
  id: string;
  title: string;
  status: SpecStatus;
  edition: number;
  version: number;
  archived: boolean;
}

export interface SpecDependencyCapabilities {
  can_remove: boolean;
  can_navigate: boolean;
  remove_reason_code: string | null;
}

/** Flat list projection returned by the board-scoped REST endpoint. */
export interface SpecDependencyItem {
  id: string;
  dependent_spec_id: string;
  prerequisite_spec_id: string;
  active: boolean;
  created_at: string;
  created_by: string;
  created_by_type: string;
  created_by_name: string | null;
  satisfied: boolean;
  resolved_on_create: boolean;
  retrospective: boolean;
  introduced_at_spec_version: number;
  source_status_on_create: SpecStatus;
  target_status_on_create: SpecStatus;
  target_version_on_create: number;
  removed_at_spec_version: number | null;
  removed_at: string | null;
  removed_by: string | null;
  removed_by_type: string | null;
  removed_by_name: string | null;
  removal_reason: string | null;
  direction: SpecDependencyDirection;
  related_spec: SpecDependencySpecSummary;
  lineage: Exclude<SpecDependencyLineageFilter, 'all'>;
  capabilities: SpecDependencyCapabilities;
}

export interface SpecDependencyBlocker {
  dependency_id: string;
  dependent_spec_id: string;
  prerequisite_spec_id: string;
  target_title: string;
  target_status: SpecStatus;
  target_edition: number;
  target_version: number;
  /** Archived prerequisites remain blockers even when their status is Done. */
  target_archived: boolean;
}

export interface SpecDependencyReadiness {
  board_id: string;
  spec_id: string;
  can_start: boolean;
  ready: boolean;
  reason_code: 'spec_dependencies_incomplete' | null;
  current_edition: number;
  last_started_edition: number | null;
  current_edition_started: boolean;
  active_dependency_count: number;
  unmet_count: number;
  blocking_count: number;
  archived_blocking_count: number;
  unfinished_blocking_count: number;
  blockers_truncated: boolean;
  blockers: SpecDependencyBlocker[];
}

export interface SpecDependencyPage {
  items: SpecDependencyItem[];
  direction: SpecDependencyDirection;
  total: number;
  has_more: boolean;
  next_cursor?: string;
  readiness: SpecDependencyReadiness;
}

export interface ListSpecDependenciesOptions {
  direction: SpecDependencyDirection;
  active_state?: SpecDependencyActiveStateFilter;
  satisfaction?: SpecDependencySatisfactionFilter;
  lineage?: SpecDependencyLineageFilter;
  related_statuses?: SpecStatus[];
  retrospective?: boolean;
  cursor?: string;
  limit?: number;
  signal?: AbortSignal;
}

export interface AddSpecDependencyRequest {
  prerequisite_spec_id: string;
  expected_spec_version: number;
  expected_spec_edition: number;
  idempotency_key: string;
}

export interface RemoveSpecDependencyRequest {
  expected_spec_version: number;
  expected_spec_edition: number;
  idempotency_key: string;
  reason: string;
}

export interface SpecDependencyMutationResponse {
  dependency: Omit<
    SpecDependencyItem,
    'direction' | 'related_spec' | 'lineage' | 'capabilities'
  >;
  spec_version: number;
  replayed: boolean;
}
