import type { Spec } from '@/types';

type LabelSource = { id: string; title?: string | null; text?: string; description?: string };
const families: Record<string, string> = { fr: 'Functional requirement', ac: 'Acceptance criterion',
  tr: 'Technical requirement', br: 'Business rule', api: 'API contract', ir: 'Integration requirement',
  or: 'Observability requirement', decision: 'Decision', scenario: 'Test scenario' };

export function deliveryItemLabel(unit: { kind: 'obligation' | 'scenario'; ref: string }, spec?: Spec | null) {
  const separator = unit.ref.indexOf(':');
  const family = unit.kind === 'scenario' ? 'scenario' : unit.ref.slice(0, separator);
  const id = unit.kind === 'scenario' ? unit.ref : unit.ref.slice(separator + 1);
  const collections: Record<string, LabelSource[] | null | undefined> = {
    fr: spec?.functional_requirements, ac: spec?.acceptance_criteria, tr: spec?.technical_requirements,
    br: spec?.business_rules, api: spec?.api_contracts, ir: spec?.integration_requirements,
    or: spec?.observability_requirements, decision: spec?.decisions, scenario: spec?.test_scenarios,
  };
  const source = collections[family]?.find(row => row.id === id);
  return { title: source?.title || source?.text || source?.description || `${families[family] || 'Assigned objective'} (${id})`,
    family: families[family] || 'Assigned objective' };
}

export function isRequirementReference(ref: string) {
  return /^(fr|ac|tr|br|api|ir|or|decision):.+$/.test(ref);
}
