import type { Spec } from '@/types';
import type { DecisionVerification } from '@/types/decision-reviews';

export function decisionObligationOptions(spec: Spec) {
  const groups = [
    ['fr', 'Functional', spec.functional_requirements], ['br', 'Business', spec.business_rules],
    ['tr', 'Technical', spec.technical_requirements], ['ir', 'Integration', spec.integration_requirements],
    ['or', 'Observability', spec.observability_requirements], ['ac', 'Acceptance', spec.acceptance_criteria],
    ['api', 'Contract', spec.api_contracts],
  ] as const;
  return groups.flatMap(([prefix, label, values]) => (values || []).filter(v =>
    !['superseded', 'revoked', 'deprecated', 'cancelled'].includes(('status' in v && v.status) || 'active'))
    .map(v => ({ ref: `${prefix}:${v.id}`, label: `${label} · ${('title' in v && v.title) || ('text' in v && v.text) || ('description' in v && v.description) || v.id}` })));
}

export function DecisionVerificationEditor({ spec, value, onChange }: {
  spec: Spec; value: DecisionVerification | null; onChange: (value: DecisionVerification | null) => void;
}) {
  const options = decisionObligationOptions(spec);
  const inspection = value?.inspection;
  const refs = value?.obligation_refs || [];
  const update = (next: DecisionVerification) => onChange(next.obligation_refs.length || next.inspection ? next : null);
  return <fieldset className="space-y-3 rounded-lg border border-gray-200 p-3 dark:border-gray-700">
    <legend className="px-1 text-sm font-semibold">How will this decision be verified?</legend>
    <p className="text-xs text-gray-500">Reuse relevant obligation evidence, inspect an observable condition, or require both.</p>
    <label className="block space-y-1 text-sm">Obligations that demonstrate this choice
      <select multiple aria-label="Verification obligations" value={refs}
        onChange={e => update({ ...value, obligation_refs: Array.from(e.target.selectedOptions, o => o.value) })}
        className="w-full rounded-lg border border-gray-300 bg-white p-2 text-sm dark:border-gray-600 dark:bg-gray-800">
        {options.map(o => <option key={o.ref} value={o.ref}>{o.label}</option>)}
      </select>
    </label>
    <label className="flex items-center gap-2 text-sm">
      <input type="checkbox" checked={!!inspection} onChange={e => update({ obligation_refs: refs,
        inspection: e.target.checked ? { condition: '', scope_refs: [{ kind: 'spec', id: spec.id }] } : null })} />
      Inspect an observable condition
    </label>
    {inspection && <>
      <label className="block space-y-1 text-sm">Expected condition
        <textarea aria-label="Expected condition" value={inspection.condition} maxLength={2000} rows={3}
          onChange={e => update({ obligation_refs: refs, inspection: { ...inspection, condition: e.target.value } })}
          className="w-full rounded-lg border border-gray-300 p-2 text-sm dark:border-gray-600 dark:bg-gray-800" />
      </label>
      <label className="block space-y-1 text-sm">Scope to inspect
        <select multiple aria-label="Inspection scope" value={inspection.scope_refs.map(r => r.kind === 'spec' ? '__spec' : r.id)}
          onChange={e => update({ obligation_refs: refs, inspection: { ...inspection, scope_refs:
            Array.from(e.target.selectedOptions, o => o.value === '__spec' ? { kind: 'spec' as const, id: spec.id } : { kind: 'obligation' as const, id: o.value }) } })}
          className="w-full rounded-lg border border-gray-300 bg-white p-2 text-sm dark:border-gray-600 dark:bg-gray-800">
          <option value="__spec">Entire specification and delivery</option>
          {options.map(o => <option key={o.ref} value={o.ref}>{o.label}</option>)}
        </select>
      </label>
    </>}
    <p className="text-xs text-gray-500">{refs.length && inspection ? 'Both paths must be satisfied.' : !refs.length && !inspection ? 'Planning pending. Select a path before validation.' : 'Task links remain contextual; no extra task or test is required for the decision.'}</p>
  </fieldset>;
}
