export interface AcceptanceCriterionView {
  key: string;
  id: string;
  label: string;
  reference: string;
  sourceIndex: number;
}

export function normalizeAcceptanceCriteria(criteria: unknown[]): AcceptanceCriterionView[] {
  const seen = new Set<string>();
  return criteria.map((criterion, index) => {
    const record = criterion && typeof criterion === 'object' ? criterion as Record<string, unknown> : {};
    if (typeof record.id !== 'string' || !record.id.trim()
      || typeof record.text !== 'string' || !record.text.trim() || seen.has(record.id)) {
      throw new Error('incompatible_spec_requirement: acceptance criterion requires unique id and text');
    }
    seen.add(record.id);
    return { key: record.id, id: record.id, label: record.text, reference: record.id, sourceIndex: index };
  });
}

export function resolveAcceptanceCriterion(
  reference: unknown, criteria: AcceptanceCriterionView[],
): AcceptanceCriterionView | undefined {
  if (typeof reference !== 'string') return undefined;
  const matches = criteria.filter((criterion) => criterion.id === reference);
  return matches.length === 1 ? matches[0] : undefined;
}

export function isAcceptanceCriterionLinked(
  linkedCriteria: readonly unknown[] | null | undefined,
  criterion: AcceptanceCriterionView,
  criteria: AcceptanceCriterionView[],
): boolean {
  if (!linkedCriteria?.length) return false;
  return linkedCriteria.some((reference) =>
    resolveAcceptanceCriterion(reference, criteria)?.key === criterion.key
  );
}

export function getAcceptanceCriterionLabel(
  reference: unknown,
  criteria: AcceptanceCriterionView[],
): string {
  return resolveAcceptanceCriterion(reference, criteria)?.label || String(reference);
}
