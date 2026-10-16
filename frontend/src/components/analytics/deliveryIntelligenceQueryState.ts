import type { DeliveryIntelligenceFilters } from './analyticsDeliveryTypes';

export function deliveryFiltersFromSearch(search: string): DeliveryIntelligenceFilters {
  const params = new URLSearchParams(search);
  const contributionView = params.get('contribution_view');
  return {
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
  if (filters.role && filters.role !== 'all') params.set('role', filters.role);
  if (filters.contributionView && filters.contributionView !== 'self_and_aggregates') {
    params.set('contribution_view', filters.contributionView);
  }
  return params;
}
