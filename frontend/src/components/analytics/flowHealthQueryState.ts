export interface FlowHealthRouteFilters {
  search: string;
  workType: string;
  owner: string;
  health: string;
  blockersOnly: boolean;
}

export const EMPTY_FLOW_HEALTH_FILTERS: FlowHealthRouteFilters = {
  search: '',
  workType: 'all',
  owner: 'all',
  health: 'all',
  blockersOnly: false,
};

export function readFlowHealthRouteFilters(search: string): FlowHealthRouteFilters {
  const params = new URLSearchParams(search);
  return {
    search: params.get('search') ?? '',
    workType: params.get('work_type') ?? 'all',
    owner: params.get('owner') ?? 'all',
    health: params.get('health') ?? 'all',
    blockersOnly: params.get('blockers_only') === 'true',
  };
}

export function analyticsQueryString(
  from: string,
  to: string,
  filters?: FlowHealthRouteFilters,
): string {
  const params = new URLSearchParams();
  params.set('from', from);
  params.set('to', to);
  if (filters) {
    if (filters.search) params.set('search', filters.search);
    if (filters.workType !== 'all') params.set('work_type', filters.workType);
    if (filters.owner !== 'all') params.set('owner', filters.owner);
    if (filters.health !== 'all') params.set('health', filters.health);
    if (filters.blockersOnly) params.set('blockers_only', 'true');
  }
  return params.toString();
}
