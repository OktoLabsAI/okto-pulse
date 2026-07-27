export interface AcceptanceCriterionView {
  key: string;
  id: string | null;
  label: string;
  reference: string;
  sourceIndex: number;
}

function asNonEmptyString(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const normalized = value.trim();
  return normalized || null;
}

export function normalizeAcceptanceCriteria(criteria: unknown[]): AcceptanceCriterionView[] {
  return criteria.map((criterion, index) => {
    const indexReference = String(index);

    if (typeof criterion === 'string') {
      const label = criterion || indexReference;
      return {
        key: `criterion-${index}`,
        id: null,
        label,
        reference: label,
        sourceIndex: index,
      };
    }

    const record = criterion && typeof criterion === 'object'
      ? criterion as Record<string, unknown>
      : {};
    const id = asNonEmptyString(record.id);
    const label = asNonEmptyString(record.text)
      || asNonEmptyString(record.title)
      || asNonEmptyString(record.name)
      || id
      || indexReference;
    const reference = id || label;

    return {
      key: id || `criterion-${index}`,
      id,
      label,
      reference,
      sourceIndex: index,
    };
  });
}

export function resolveAcceptanceCriterion(
  reference: unknown,
  criteria: AcceptanceCriterionView[],
): AcceptanceCriterionView | undefined {
  if (typeof reference === 'boolean' || reference === null || reference === undefined) {
    return undefined;
  }

  if (typeof reference === 'number') {
    if (!Number.isInteger(reference)) return undefined;
    return criteria.find((criterion) => criterion.sourceIndex === reference);
  }

  const token = String(reference).trim();
  if (!token) return undefined;
  if (/^-?\d+$/.test(token)) {
    const sourceIndex = Number(token);
    return criteria.find((criterion) => criterion.sourceIndex === sourceIndex);
  }

  return criteria.find((criterion) => criterion.id === token)
    || criteria.find((criterion) => criterion.label === token)
    || criteria.find((criterion) =>
      criterion.label.startsWith(token) || token.startsWith(criterion.label)
    );
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
