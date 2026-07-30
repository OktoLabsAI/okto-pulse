export const CONTEXTUAL_HELP_EVENT = 'okto:open-help';

export const HELP_SECTION_IDS = [
  'guided-tours',
  'quickstart',
  'agents',
  'ideations',
  'refinements',
  'specs',
  'tasks',
  'bugs',
  'analytics',
  'guidelines',
  'policy-governance',
  'governance',
  'sprints',
  'knowledge-graph',
  'board-settings',
  'curated-spec-checklist',
  'requirement-lint',
  'permissions',
  'collaboration',
] as const;

export type HelpSectionId = (typeof HELP_SECTION_IDS)[number];

export interface OpenContextualHelpDetail {
  sectionId: HelpSectionId;
}

export type ContextualHelpListener = (
  detail: OpenContextualHelpDetail,
) => void;

export function isHelpSectionId(value: unknown): value is HelpSectionId {
  return typeof value === 'string'
    && (HELP_SECTION_IDS as readonly string[]).includes(value);
}

export function openContextualHelp(sectionId: HelpSectionId): void {
  if (typeof window === 'undefined') return;
  window.dispatchEvent(
    new CustomEvent<OpenContextualHelpDetail>(CONTEXTUAL_HELP_EVENT, {
      detail: { sectionId },
    }),
  );
}

export function subscribeContextualHelp(
  listener: ContextualHelpListener,
): () => void {
  if (typeof window === 'undefined') return () => undefined;

  const handler = (event: Event) => {
    const detail = (event as CustomEvent<OpenContextualHelpDetail>).detail;
    if (!detail || !isHelpSectionId(detail.sectionId)) return;
    listener(detail);
  };
  window.addEventListener(CONTEXTUAL_HELP_EVENT, handler);
  return () => window.removeEventListener(CONTEXTUAL_HELP_EVENT, handler);
}
