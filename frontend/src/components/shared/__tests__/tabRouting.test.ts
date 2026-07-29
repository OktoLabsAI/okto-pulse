import { describe, expect, it } from 'vitest';

import {
  IDEATION_LEGACY_TAB_ALIASES,
  REFINEMENT_LEGACY_TAB_ALIASES,
  SPEC_LEGACY_TAB_ALIASES,
  resolveTabTarget,
  type IdeationModalSubtab,
  type IdeationModalTab,
  type SpecModalSubtab,
  type SpecModalTab,
} from '../tabRouting';

describe('tabRouting', () => {
  it('maps legacy Ideation tabs into their canonical grouped workspaces', () => {
    const options = {
      aliases: IDEATION_LEGACY_TAB_ALIASES,
      visibleTabs: [
        'details',
        'resources',
        'qa',
        'evaluation',
        'references',
        'versions',
        'activity',
      ] as const,
      visibleSubtabs: {
        resources: ['mockups', 'knowledge', 'architecture'],
        evaluation: ['scope', 'ambiguity'],
        references: ['stories', 'refinements', 'specs'],
      } satisfies Partial<
        Record<IdeationModalTab, readonly IdeationModalSubtab[]>
      >,
      fallback: { tab: 'details' as const },
    };

    expect(resolveTabTarget('quality', options)).toEqual({
      tab: 'evaluation',
      subtab: 'ambiguity',
    });
    expect(resolveTabTarget('stories', options)).toEqual({
      tab: 'references',
      subtab: 'stories',
    });
    expect(resolveTabTarget('architecture', options)).toEqual({
      tab: 'resources',
      subtab: 'architecture',
    });
    expect(resolveTabTarget('cancellation', options)).toEqual({
      tab: 'details',
      anchorId: 'cancellation-panel',
    });
  });

  it('resolves aliases before permission fallback and chooses a visible subtab', () => {
    const resolved = resolveTabTarget<SpecModalTab, SpecModalSubtab>(
      'quality',
      {
        aliases: SPEC_LEGACY_TAB_ALIASES,
        visibleTabs: ['details', 'validation'],
        visibleSubtabs: {
          validation: ['checklist', 'spec-validation'],
        },
        fallback: { tab: 'details' },
      },
    );

    expect(resolved).toEqual({
      tab: 'validation',
      subtab: 'checklist',
    });
  });

  it('falls back deterministically when the canonical parent is not visible', () => {
    expect(
      resolveTabTarget<SpecModalTab, SpecModalSubtab>('quality', {
        aliases: SPEC_LEGACY_TAB_ALIASES,
        visibleTabs: ['details', 'tests'],
        fallback: { tab: 'details' },
      }),
    ).toEqual({ tab: 'details' });
  });

  it('accepts canonical parent/subtab paths for programmatic navigation', () => {
    expect(
      resolveTabTarget<SpecModalTab, SpecModalSubtab>(
        'resources/knowledge',
        {
          aliases: SPEC_LEGACY_TAB_ALIASES,
          visibleTabs: ['details', 'resources'],
          visibleSubtabs: {
            resources: ['mockups', 'knowledge', 'architecture'],
          },
          fallback: { tab: 'details' },
        },
      ),
    ).toEqual({ tab: 'resources', subtab: 'knowledge' });
  });

  it('selects the first visible subtab for a canonical parent target', () => {
    expect(
      resolveTabTarget<SpecModalTab, SpecModalSubtab>('validation', {
        aliases: SPEC_LEGACY_TAB_ALIASES,
        visibleTabs: ['details', 'validation'],
        visibleSubtabs: {
          validation: ['requirement-lint', 'checklist'],
        },
        fallback: { tab: 'details' },
      }),
    ).toEqual({
      tab: 'validation',
      subtab: 'requirement-lint',
    });
  });

  it('keeps the agreed Refinement aliases and excludes Spec Versions', () => {
    expect(REFINEMENT_LEGACY_TAB_ALIASES.decisions).toEqual({
      tab: 'research-decisions',
    });
    expect(
      Object.values(SPEC_LEGACY_TAB_ALIASES).some(
        (target) => target.tab === ('versions' as SpecModalTab),
      ),
    ).toBe(false);
    expect(SPEC_LEGACY_TAB_ALIASES.history).toEqual({ tab: 'activity' });
  });
});
