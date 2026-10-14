export type ReadinessDestination = { type: 'card' | 'spec' | 'scenario' | 'obligation' | 'target' | 'record'; id: string };
export interface ReadinessNavigator {
  open: (destination: ReadinessDestination) => void;
  supports?: (destination: ReadinessDestination) => boolean;
}
export type ReadinessSpecTab = 'details' | 'frs' | 'acs' | 'rules' | 'contracts' | 'irs' | 'ors' | 'trs' | 'decisions' | 'tests';
export interface ReadinessSpecLocation { tab: ReadinessSpecTab; ref: string | null }

const sections: Record<string, ReadinessSpecTab> = {
  fr: 'frs', ac: 'acs', br: 'rules', api: 'contracts', ir: 'irs', or: 'ors', tr: 'trs', decision: 'decisions',
};

/** Only canonical references select a native section. Never guess an entity from its title. */
export function readinessSpecLocation(destination: ReadinessDestination, specId: string): ReadinessSpecLocation | null {
  if (!destination.id) return null;
  if (destination.type === 'spec') return destination.id === specId ? { tab: 'details', ref: null } : null;
  if (destination.type === 'scenario') return { tab: 'tests', ref: `scenario:${destination.id}` };
  if (destination.type !== 'obligation') return null;
  const separator = destination.id.indexOf(':');
  if (separator < 1 || separator === destination.id.length - 1) return null;
  const tab = sections[destination.id.slice(0, separator)];
  return tab ? { tab, ref: destination.id } : null;
}
