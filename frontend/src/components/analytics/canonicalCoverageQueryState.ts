export type CanonicalCoverageOutcome =
  | 'all'
  | 'covered'
  | 'uncovered'
  | 'skipped'
  | 'incomplete';

export interface CanonicalCoverageQueryState {
  from: string;
  to: string;
  lifecycle: string;
  outcome: CanonicalCoverageOutcome;
  search: string;
}

export const CANONICAL_COVERAGE_ROUTE_SUFFIX = 'canonical-coverage';

const OUTCOMES = new Set<CanonicalCoverageOutcome>([
  'all',
  'covered',
  'uncovered',
  'skipped',
  'incomplete',
]);

function boundedText(value: string | null | undefined, fallback: string): string {
  const normalized = value?.trim();
  return normalized ? normalized.slice(0, 512) : fallback;
}

function outcome(value: string | null | undefined): CanonicalCoverageOutcome {
  return OUTCOMES.has(value as CanonicalCoverageOutcome)
    ? value as CanonicalCoverageOutcome
    : 'all';
}

export function canonicalCoverageQueryState(
  value: Partial<CanonicalCoverageQueryState> & Pick<CanonicalCoverageQueryState, 'from' | 'to'>,
): CanonicalCoverageQueryState {
  return {
    from: boundedText(value.from, ''),
    to: boundedText(value.to, ''),
    lifecycle: boundedText(value.lifecycle, 'all'),
    outcome: outcome(value.outcome),
    search: value.search?.slice(0, 512) ?? '',
  };
}

export function parseCanonicalCoverageQuery(
  search: string | URLSearchParams,
  fallback: Pick<CanonicalCoverageQueryState, 'from' | 'to'>,
): CanonicalCoverageQueryState {
  const params = typeof search === 'string'
    ? new URLSearchParams(search.startsWith('?') ? search.slice(1) : search)
    : search;
  return canonicalCoverageQueryState({
    from: params.get('from') ?? fallback.from,
    to: params.get('to') ?? fallback.to,
    lifecycle: params.get('lifecycle') ?? 'all',
    outcome: outcome(params.get('outcome')),
    search: params.get('search')?.trim() ?? '',
  });
}

export function canonicalCoverageSearchParams(
  state: CanonicalCoverageQueryState,
): URLSearchParams {
  const normalized = canonicalCoverageQueryState(state);
  const params = new URLSearchParams();
  params.set('from', normalized.from);
  params.set('to', normalized.to);
  params.set('lifecycle', normalized.lifecycle);
  params.set('outcome', normalized.outcome);
  if (normalized.search) params.set('search', normalized.search);
  return params;
}

export function canonicalCoverageFullViewPath(
  boardId: string,
  state: CanonicalCoverageQueryState,
): string {
  return `/analytics/boards/${encodeURIComponent(boardId)}/${CANONICAL_COVERAGE_ROUTE_SUFFIX}?${canonicalCoverageSearchParams(state).toString()}`;
}
