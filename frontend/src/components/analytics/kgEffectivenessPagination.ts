import {
  BOARD_KG_COGNITIVE_STATUSES,
  type BoardKgAnalyticsResponse,
  type BoardKgAnalyticsState,
  type BoardKgDomainAge,
  type BoardKgEffectivenessState,
  type BoardKgProvenanceKind,
} from './analyticsCanonicalTypes';

function mergeAnalyticsState(states: readonly BoardKgAnalyticsState[]): BoardKgAnalyticsState {
  const precedence: BoardKgAnalyticsState[] = ['error', 'restricted', 'unavailable', 'partial', 'available', 'empty'];
  return precedence.find((state) => states.includes(state)) ?? 'unavailable';
}

function mergeEffectivenessState(states: readonly BoardKgEffectivenessState[]): BoardKgEffectivenessState {
  if (states.includes('restricted')) return 'restricted';
  if (states.includes('unavailable')) return 'unavailable';
  if (states.includes('available')) return 'available';
  return 'empty';
}

function sumNullable(values: readonly (number | null)[]): number | null {
  if (values.some((value) => value === null)) return null;
  return (values as readonly number[]).reduce((total, value) => total + value, 0);
}

function exactRate(numerator: number | null, denominator: number | null): number | null {
  if (numerator === null || denominator === null || denominator === 0) return null;
  return numerator / denominator;
}

function mergePagedAge(ages: readonly BoardKgDomainAge[]): BoardKgDomainAge {
  const sampleCount = ages.reduce((total, age) => total + age.sample_count, 0);
  if (ages.length === 1) return ages[0];
  const oldest = ages.flatMap((age) => age.oldest_hours === null ? [] : [age.oldest_hours]);
  if (sampleCount === 0 && ages.every((age) => age.result_state === 'empty')) {
    return {
      result_state: 'empty',
      sample_count: 0,
      p50_hours: null,
      p95_hours: null,
      oldest_hours: null,
      reason: null,
    };
  }
  return {
    result_state: 'partial',
    sample_count: sampleCount,
    p50_hours: null,
    p95_hours: null,
    oldest_hours: oldest.length > 0 ? Math.max(...oldest) : null,
    reason: 'Raw samples are not exposed across pages, so page quantiles were not combined.',
  };
}

function uniqueDiagnostics(pages: readonly BoardKgAnalyticsResponse[]) {
  const seen = new Set<string>();
  return pages.flatMap((page) => page.diagnostics).filter((diagnostic) => {
    const key = JSON.stringify(diagnostic);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function uniqueStrings(values: readonly string[]): string[] {
  return [...new Set(values.map((value) => value.trim()).filter(Boolean))].sort();
}

/**
 * Cursor pages contain disjoint cognitive records but repeat operational KG facts.
 * Only additive cognitive facts are combined. Quantiles deliberately fail closed
 * because the canonical response does not expose the raw samples needed to merge them.
 */
export function mergeBoardKgAnalyticsPages(
  pages: readonly BoardKgAnalyticsResponse[],
): BoardKgAnalyticsResponse | null {
  if (pages.length === 0) return null;
  if (pages.length === 1) return pages[0];

  const first = pages[0];
  const latest = pages[pages.length - 1];
  const inventoryTotal = sumNullable(pages.map((page) => page.cognitive_inventory.total));
  const overdueRevisits = sumNullable(pages.map((page) => page.cognitive_inventory.overdue_revisits));
  const numerator = sumNullable(pages.map((page) => page.effectiveness.numerator));
  const denominator = sumNullable(pages.map((page) => page.effectiveness.denominator));
  const candidateCount = sumNullable(pages.map((page) => page.effectiveness.candidate_count));
  const persistedCount = sumNullable(pages.map((page) => page.effectiveness.persisted_count));
  const provenanceTotal = sumNullable(pages.map((page) => page.provenance_mix.total));
  const inventoryByStatus = inventoryTotal === null ? {} : Object.fromEntries(
    BOARD_KG_COGNITIVE_STATUSES.map((status) => [
      status,
      pages.reduce((total, page) => total + (page.cognitive_inventory.by_status[status] ?? 0), 0),
    ]),
  );
  const provenanceKinds: BoardKgProvenanceKind[] = ['deterministic', 'cognitive', 'fallback', 'legacy'];
  const provenanceByKind = provenanceTotal === null ? {} : Object.fromEntries(provenanceKinds.map((kind) => {
    const count = pages.reduce((total, page) => total + (page.provenance_mix.by_kind[kind]?.count ?? 0), 0);
    return [kind, { count, rate: exactRate(count, provenanceTotal) }];
  }));
  const cognitiveDomains = pages.flatMap((page) => page.domains.filter((domain) => domain.domain === 'cognitive_backlog'));

  return {
    ...first,
    as_of: latest.as_of,
    query_fingerprint: latest.query_fingerprint,
    query: { ...first.query, cursor: null },
    result_state: mergeAnalyticsState(pages.map((page) => page.result_state)),
    provenance: {
      ...latest.provenance,
      sources: Array.from(
        new Map(
          pages.flatMap((page) => page.provenance.sources).map((source) => [JSON.stringify(source), source]),
        ).values(),
      ),
    },
    domains: first.domains.map((domain) => {
      if (domain.domain !== 'cognitive_backlog' || cognitiveDomains.length === 0) return domain;
      return {
        ...domain,
        result_state: mergeAnalyticsState(cognitiveDomains.map((item) => item.result_state)),
        count: sumNullable(cognitiveDomains.map((item) => item.count)),
        age: mergePagedAge(cognitiveDomains.map((item) => item.age)),
        reason: cognitiveDomains.find((item) => item.reason)?.reason ?? null,
      };
    }),
    cognitive_inventory: {
      ...first.cognitive_inventory,
      result_state: mergeAnalyticsState(pages.map((page) => page.cognitive_inventory.result_state)),
      by_status: inventoryByStatus,
      total: inventoryTotal,
      overdue_revisits: overdueRevisits,
      age: mergePagedAge(pages.map((page) => page.cognitive_inventory.age)),
      reason: pages.find((page) => page.cognitive_inventory.reason)?.cognitive_inventory.reason ?? null,
    },
    effectiveness: {
      ...first.effectiveness,
      state: mergeEffectivenessState(pages.map((page) => page.effectiveness.state)),
      numerator,
      denominator,
      rate: exactRate(numerator, denominator),
      candidate_count: candidateCount,
      persisted_count: persistedCount,
      conversion_rate: exactRate(persistedCount, candidateCount),
      timing: {
        state: 'unavailable',
        sample_count: pages.reduce((total, page) => total + page.effectiveness.timing.sample_count, 0),
        p50_hours: null,
        p95_hours: null,
        reason: 'Raw timing samples are not exposed across pages, so page quantiles were not combined.',
      },
      reason: pages.find((page) => page.effectiveness.reason)?.effectiveness.reason ?? null,
    },
    provenance_mix: {
      ...first.provenance_mix,
      result_state: mergeAnalyticsState(pages.map((page) => page.provenance_mix.result_state)),
      total: provenanceTotal,
      by_kind: provenanceByKind,
      reason: pages.find((page) => page.provenance_mix.reason)?.provenance_mix.reason ?? null,
    },
    diagnostics: uniqueDiagnostics(pages),
    redactions: uniqueStrings(pages.flatMap((page) => page.redactions)),
    next_cursor: latest.next_cursor,
  };
}
