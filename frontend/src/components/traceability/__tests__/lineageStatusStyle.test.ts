import { describe, expect, it } from 'vitest';
import { CARD_STATUSES, STORY_STATUSES, IDEATION_STATUSES, REFINEMENT_STATUSES, SPEC_STATUSES } from '@/types';
import { LINEAGE_STATUS_COLORS, lineageStatusColor } from '../lineageStatusStyle';

describe('lineage status colors', () => {
  it('covers all current entity lifecycles with a distinct stable color per status', () => {
    const statuses = new Set([...CARD_STATUSES, ...STORY_STATUSES, ...IDEATION_STATUSES, ...REFINEMENT_STATUSES, ...SPEC_STATUSES]);
    for (const status of statuses) expect(LINEAGE_STATUS_COLORS[status]).toMatch(/^#[0-9a-f]{6}$/);
    expect(new Set(Object.values(LINEAGE_STATUS_COLORS)).size).toBe(Object.keys(LINEAGE_STATUS_COLORS).length);
  });
  it('normalizes known values and provides a neutral unknown/missing fallback', () => {
    expect(lineageStatusColor(' APPROVED ')).toBe(lineageStatusColor('approved'));
    expect(lineageStatusColor(null)).toBe('#64748b');
    expect(lineageStatusColor('future_status')).toBe('#64748b');
  });
});
