import { describe, expect, it } from 'vitest';
import {
  deliveryFiltersFromSearch,
  deliveryFiltersToSearch,
} from './deliveryIntelligenceQueryState';

describe('Delivery Intelligence query state', () => {
  it('parses the governed URL filters and restores the fixed page limit', () => {
    expect(deliveryFiltersFromSearch(
      '?sprint_id=sprint%2Fone&lane=hotfix&role=quality_engineer&contribution_view=operator&limit=999&cursor=opaque',
    )).toEqual({
      sprintId: 'sprint/one',
      lane: 'hotfix',
      role: 'quality_engineer',
      contributionView: 'operator',
      limit: 25,
    });
  });

  it('fails closed to safe defaults for unknown enum values', () => {
    expect(deliveryFiltersFromSearch('?lane=fabricated&contribution_view=everyone')).toEqual({
      sprintId: undefined,
      lane: 'all',
      role: 'all',
      contributionView: 'self_and_aggregates',
      limit: 25,
    });
  });

  it('serializes only durable non-default filters and round-trips them', () => {
    const params = deliveryFiltersToSearch({
      sprintId: 'sprint/one',
      lane: 'normal',
      role: 'developer',
      contributionView: 'aggregates',
      cursor: 'server-only-cursor',
      limit: 500,
    });
    expect(params.toString()).toBe(
      'sprint_id=sprint%2Fone&lane=normal&role=developer&contribution_view=aggregates',
    );
    expect(deliveryFiltersFromSearch(params.toString())).toEqual({
      sprintId: 'sprint/one',
      lane: 'normal',
      role: 'developer',
      contributionView: 'aggregates',
      limit: 25,
    });

    expect(deliveryFiltersToSearch({
      lane: 'all',
      role: 'all',
      contributionView: 'self_and_aggregates',
      limit: 25,
    }).toString()).toBe('');
  });
});
