import { describe, expect, it } from 'vitest';

import {
  ENTITY_CHIP_COLORS,
  ENTITY_COLORS,
  ENTITY_LABELS,
  getEntityChipClasses,
  getEntityTextClasses,
  type PermissionEntity,
} from './permissionLabels';

function classTokens(classes: string): string[] {
  return classes.split(/\s+/).filter(Boolean);
}

function hasVariant(classes: string, prefix: string): boolean {
  return classTokens(classes).some((token) => token.startsWith(prefix));
}

describe('permission entity visual tokens', () => {
  it('covers every canonical label with exactly one text and chip palette', () => {
    const canonicalEntities = Object.keys(ENTITY_LABELS).sort();

    expect(Object.keys(ENTITY_COLORS).sort()).toEqual(canonicalEntities);
    expect(Object.keys(ENTITY_CHIP_COLORS).sort()).toEqual(canonicalEntities);
  });

  it.each(Object.keys(ENTITY_LABELS) as PermissionEntity[])(
    'provides complete light and dark tokens for %s',
    (entity) => {
      const textClasses = ENTITY_COLORS[entity];
      const chipClasses = ENTITY_CHIP_COLORS[entity];

      expect(textClasses).toBeTruthy();
      expect(hasVariant(textClasses, 'text-')).toBe(true);
      expect(hasVariant(textClasses, 'dark:text-')).toBe(true);

      expect(chipClasses).toBeTruthy();
      expect(hasVariant(chipClasses, 'bg-')).toBe(true);
      expect(hasVariant(chipClasses, 'text-')).toBe(true);
      expect(hasVariant(chipClasses, 'dark:bg-')).toBe(true);
      expect(hasVariant(chipClasses, 'dark:text-')).toBe(true);

      expect(getEntityTextClasses(entity)).toBe(textClasses);
      expect(getEntityChipClasses(entity)).toBe(chipClasses);
    },
  );

  it('provides dark-mode-safe fallbacks for future entities', () => {
    const textClasses = getEntityTextClasses('future_policy_group');
    const chipClasses = getEntityChipClasses('future_policy_group');

    expect(hasVariant(textClasses, 'text-')).toBe(true);
    expect(hasVariant(textClasses, 'dark:text-')).toBe(true);
    expect(hasVariant(chipClasses, 'bg-')).toBe(true);
    expect(hasVariant(chipClasses, 'text-')).toBe(true);
    expect(hasVariant(chipClasses, 'dark:bg-')).toBe(true);
    expect(hasVariant(chipClasses, 'dark:text-')).toBe(true);
  });
});
