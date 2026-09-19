import type { VerificationRequirementOption, VerificationRequirementType } from './CriterionVerificationPanel';

export function verificationRequirementOptions(collections: {
  functional_requirements?: unknown[] | null; technical_requirements?: unknown[] | null;
  integration_requirements?: unknown[] | null; observability_requirements?: unknown[] | null;
  business_rules?: unknown[] | null;
}, canReadIR: boolean, canReadOR: boolean): VerificationRequirementOption[] {
  const groups: [VerificationRequirementType, unknown[] | null | undefined][] = [
    ['functional_requirement', collections.functional_requirements],
    ['technical_requirement', collections.technical_requirements],
    ['business_rule', collections.business_rules],
    ['integration_requirement', canReadIR ? collections.integration_requirements : []],
    ['observability_requirement', canReadOR ? collections.observability_requirements : []],
  ];
  return groups.flatMap(([type, values]) => (values || []).flatMap(value => {
    if (!value || typeof value !== 'object') return [];
    const row = value as Record<string, unknown>;
    if (typeof row.id !== 'string' || !row.id || (row.status != null && row.status !== 'active')) return [];
    return [{ type, id: row.id, title: String(row.text || row.title || row.rule || row.id) }];
  }));
}

