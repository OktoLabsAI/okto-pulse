import { describe, expect, it } from 'vitest';

import {
  CARD_LEGACY_TAB_ALIASES,
  IDEATION_LEGACY_TAB_ALIASES,
  REFINEMENT_LEGACY_TAB_ALIASES,
  SPEC_LEGACY_TAB_ALIASES,
  resolveTabTarget,
  type CardModalSubtab,
  type CardModalTab,
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

  it('maps legacy Card tabs into the canonical grouped workspaces', () => {
    const options = {
      aliases: CARD_LEGACY_TAB_ALIASES,
      visibleTabs: [
        'details',
        'tests',
        'resources',
        'qa',
        'comments',
        'references',
        'validation',
        'activity',
      ] as const,
      visibleSubtabs: {
        tests: ['regression', 'coverage', 'amendment', 'scenarios', 'evidence'],
        resources: ['mockups', 'knowledge', 'architecture', 'attachments'],
        references: ['lineage', 'requirements', 'dependencies'],
        validation: ['execution-report', 'task-validation'],
      } satisfies Partial<
        Record<CardModalTab, readonly CardModalSubtab[]>
      >,
      fallback: { tab: 'details' as const },
    };

    expect(resolveTabTarget('evidence', options)).toEqual({
      tab: 'tests',
      subtab: 'evidence',
    });
    expect(resolveTabTarget('architecture', options)).toEqual({
      tab: 'resources',
      subtab: 'architecture',
    });
    expect(resolveTabTarget('conclusion', options)).toEqual({
      tab: 'validation',
      subtab: 'execution-report',
    });
    expect(resolveTabTarget('validations', options)).toEqual({
      tab: 'validation',
      subtab: 'task-validation',
    });
    expect(resolveTabTarget('cancellation', options)).toEqual({
      tab: 'details',
      anchorId: 'cancellation-panel',
    });
    expect(resolveTabTarget('history', options)).toEqual({
      tab: 'activity',
    });
  });

  it('lets each Card type resolve the canonical Tests parent to its first visible subtab', () => {
    expect(
      resolveTabTarget<CardModalTab, CardModalSubtab>('tests', {
        aliases: CARD_LEGACY_TAB_ALIASES,
        visibleTabs: ['details', 'tests'],
        visibleSubtabs: {
          tests: ['regression', 'coverage'],
        },
        fallback: { tab: 'details' },
      }),
    ).toEqual({ tab: 'tests', subtab: 'regression' });

    expect(
      resolveTabTarget<CardModalTab, CardModalSubtab>('tests', {
        aliases: CARD_LEGACY_TAB_ALIASES,
        visibleTabs: ['details', 'tests'],
        visibleSubtabs: {
          tests: ['scenarios', 'evidence'],
        },
        fallback: { tab: 'details' },
      }),
    ).toEqual({ tab: 'tests', subtab: 'scenarios' });
  });

  it('falls back from a Card alias when its canonical workspace is not visible', () => {
    expect(
      resolveTabTarget<CardModalTab, CardModalSubtab>('validations', {
        aliases: CARD_LEGACY_TAB_ALIASES,
        visibleTabs: ['details', 'activity'],
        fallback: { tab: 'details' },
      }),
    ).toEqual({ tab: 'details' });
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
