import type { DeliveryIntelligenceFilters } from './analyticsDeliveryTypes';

export function deliveryFiltersFromSearch(search: string): DeliveryIntelligenceFilters {
  const params = new URLSearchParams(search);
  const lane = params.get('lane');
  const contributionView = params.get('contribution_view');
  return {
    sprintId: params.get('sprint_id') || undefined,
    lane: lane === 'normal' || lane === 'hotfix' ? lane : 'all',
    role: params.get('role') || 'all',
    contributionView:
      contributionView === 'self'
      || contributionView === 'aggregates'
      || contributionView === 'operator'
        ? contributionView
        : 'self_and_aggregates',
    limit: 25,
  };
}

export function deliveryFiltersToSearch(filters: DeliveryIntelligenceFilters): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.sprintId) params.set('sprint_id', filters.sprintId);
  if (filters.lane && filters.lane !== 'all') params.set('lane', filters.lane);
  if (filters.role && filters.role !== 'all') params.set('role', filters.role);
  if (filters.contributionView && filters.contributionView !== 'self_and_aggregates') {
    params.set('contribution_view', filters.contributionView);
  }
  return params;
}
