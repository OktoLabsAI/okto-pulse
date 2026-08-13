export interface TabTarget<
  TTop extends string = string,
  TSub extends string = string,
> {
  tab: TTop;
  subtab?: TSub;
  anchorId?: string;
}

export type TabAliasMap<
  TTop extends string = string,
  TSub extends string = string,
> = Readonly<Record<string, TabTarget<TTop, TSub>>>;

export interface ResolveTabTargetOptions<
  TTop extends string,
  TSub extends string,
> {
  aliases?: TabAliasMap<TTop, TSub>;
  visibleTabs: readonly TTop[];
  visibleSubtabs?: Partial<Record<TTop, readonly TSub[]>>;
  fallback: TabTarget<TTop, TSub>;
}

export type IdeationModalTab =
  | 'details'
  | 'resources'
  | 'qa'
  | 'evaluation'
  | 'references'
  | 'versions'
  | 'activity';

export type IdeationModalSubtab =
  | 'mockups'
  | 'knowledge'
  | 'architecture'
  | 'scope'
  | 'ambiguity'
  | 'policy-compliance'
  | 'stories'
  | 'refinements'
  | 'specs';

export type RefinementModalTab =
  | 'details'
  | 'research-decisions'
  | 'code-evidence'
  | 'resources'
  | 'qa'
  | 'references'
  | 'validation'
  | 'versions'
  | 'activity';

export type RefinementModalSubtab =
  | 'mockups'
  | 'knowledge'
  | 'architecture'
  | 'ideation'
  | 'specs'
  | 'ambiguity'
  | 'policy-compliance';

export type SpecModalTab =
  | 'details'
  | 'evidence-matrix'
  | 'tests'
  | 'rules'
  | 'dependencies'
  | 'contracts'
  | 'irs'
  | 'ors'
  | 'trs'
  | 'decisions'
  | 'resources'
  | 'qa'
  | 'references'
  | 'sprints'
  | 'kg'
  | 'validation'
  | 'activity';

export type SpecModalSubtab =
  | 'mockups'
  | 'knowledge'
  | 'architecture'
  | 'origin'
  | 'cards'
  | 'checklist'
  | 'spec-validation'
  | 'requirement-lint'
  | 'policy-compliance';

export type CardModalTab =
  | 'details'
  | 'implementation-targets'
  | 'tests'
  | 'resources'
  | 'qa'
  | 'comments'
  | 'references'
  | 'validation'
  | 'activity';

export type CardModalSubtab =
  | 'regression'
  | 'coverage'
  | 'amendment'
  | 'scenarios'
  | 'evidence'
  | 'mockups'
  | 'knowledge'
  | 'architecture'
  | 'attachments'
  | 'lineage'
  | 'requirements'
  | 'dependencies'
  | 'execution-report'
  | 'task-validation'
  | 'policy-compliance';

export const IDEATION_LEGACY_TAB_ALIASES = {
  quality: { tab: 'evaluation', subtab: 'ambiguity' },
  stories: { tab: 'references', subtab: 'stories' },
  refinements: { tab: 'references', subtab: 'refinements' },
  mockups: { tab: 'resources', subtab: 'mockups' },
  knowledge: { tab: 'resources', subtab: 'knowledge' },
  architecture: { tab: 'resources', subtab: 'architecture' },
  cancellation: { tab: 'details', anchorId: 'cancellation-panel' },
  history: { tab: 'activity' },
} as const satisfies TabAliasMap<IdeationModalTab, IdeationModalSubtab>;

export const REFINEMENT_LEGACY_TAB_ALIASES = {
  decisions: { tab: 'research-decisions' },
  quality: { tab: 'validation', subtab: 'ambiguity' },
  specs: { tab: 'references', subtab: 'specs' },
  mockups: { tab: 'resources', subtab: 'mockups' },
  knowledge: { tab: 'resources', subtab: 'knowledge' },
  architecture: { tab: 'resources', subtab: 'architecture' },
  cancellation: { tab: 'details', anchorId: 'cancellation-panel' },
  history: { tab: 'activity' },
} as const satisfies TabAliasMap<RefinementModalTab, RefinementModalSubtab>;

export const SPEC_LEGACY_TAB_ALIASES = {
  quality: { tab: 'validation', subtab: 'requirement-lint' },
  cards: { tab: 'references', subtab: 'cards' },
  mockups: { tab: 'resources', subtab: 'mockups' },
  knowledge: { tab: 'resources', subtab: 'knowledge' },
  architecture: { tab: 'resources', subtab: 'architecture' },
  cancellation: { tab: 'details', anchorId: 'cancellation-panel' },
  history: { tab: 'activity' },
} as const satisfies TabAliasMap<SpecModalTab, SpecModalSubtab>;

export const CARD_LEGACY_TAB_ALIASES = {
  evidence: { tab: 'tests', subtab: 'evidence' },
  mockups: { tab: 'resources', subtab: 'mockups' },
  knowledge: { tab: 'resources', subtab: 'knowledge' },
  architecture: { tab: 'resources', subtab: 'architecture' },
  conclusion: { tab: 'validation', subtab: 'execution-report' },
  validations: { tab: 'validation', subtab: 'task-validation' },
  cancellation: { tab: 'details', anchorId: 'cancellation-panel' },
  history: { tab: 'activity' },
} as const satisfies TabAliasMap<CardModalTab, CardModalSubtab>;

function parseRequestedTarget<TTop extends string, TSub extends string>(
  requested: string | TabTarget<TTop, TSub> | null | undefined,
  aliases: TabAliasMap<TTop, TSub>,
): TabTarget<TTop, TSub> | null {
  if (!requested) return null;
  if (typeof requested !== 'string') return requested;

  const normalized = requested.trim().replace(/^#/, '');
  if (!normalized) return null;
  if (aliases[normalized]) return aliases[normalized];

  const [tab, subtab] = normalized.split('/', 2);
  return {
    tab: tab as TTop,
    ...(subtab ? { subtab: subtab as TSub } : {}),
  };
}

function firstVisibleTarget<TTop extends string, TSub extends string>(
  preferred: TabTarget<TTop, TSub>,
  visibleTabs: readonly TTop[],
  visibleSubtabs: Partial<Record<TTop, readonly TSub[]>>,
): TabTarget<TTop, TSub> {
  const tab = visibleTabs.includes(preferred.tab)
    ? preferred.tab
    : visibleTabs[0];
  if (!tab) return preferred;

  const subtabs = visibleSubtabs[tab];
  if (!subtabs || subtabs.length === 0) {
    return { tab };
  }
  const subtab = preferred.tab === tab
    && preferred.subtab
    && subtabs.includes(preferred.subtab)
    ? preferred.subtab
    : subtabs[0];
  return { tab, subtab };
}

/**
 * Resolves legacy/programmatic tab identifiers into a visible canonical
 * target. Resolution always happens before permission/status fallback.
 */
export function resolveTabTarget<
  TTop extends string,
  TSub extends string = string,
>(
  requested: string | TabTarget<TTop, TSub> | null | undefined,
  {
    aliases = {},
    visibleTabs,
    visibleSubtabs = {},
    fallback,
  }: ResolveTabTargetOptions<TTop, TSub>,
): TabTarget<TTop, TSub> {
  const target = parseRequestedTarget(requested, aliases);
  if (!target || !visibleTabs.includes(target.tab)) {
    return firstVisibleTarget(fallback, visibleTabs, visibleSubtabs);
  }

  const allowedSubtabs = visibleSubtabs[target.tab];
  if (!allowedSubtabs || allowedSubtabs.length === 0) {
    return target;
  }
  if (!target.subtab) {
    return {
      ...target,
      subtab: allowedSubtabs[0],
    };
  }
  if (allowedSubtabs.includes(target.subtab)) {
    return target;
  }

  return {
    tab: target.tab,
    subtab: allowedSubtabs[0],
  };
}
