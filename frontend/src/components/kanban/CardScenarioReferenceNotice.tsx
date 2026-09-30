import type { Card } from '../../types';

const reasons = {
  parent_absent: 'The referenced scenario has no available parent Spec.',
  target_absent: 'The referenced scenario no longer exists in the parent Spec.',
  target_ambiguous: 'The parent Spec contains more than one scenario with this ID.',
};

export function CardScenarioReferenceNotice({ context }: { context: Card['scenario_reference_context'] }) {
  if (context?.status === 'not_authorized') return null;
  if (!context || context.status === 'unavailable') {
    return <p role="status" className="text-xs text-gray-500">Scenario reference checks are unavailable.</p>;
  }
  if (context.finding_count === 0) return null;
  return (
    <section aria-label="Scenario reference issues" className="rounded border border-amber-300 p-3 text-sm dark:border-amber-700">
      <p className="font-medium">{context.finding_count} scenario reference issue(s)</p>
      <ul className="mt-2 space-y-2">
        {context.findings.map((finding) => (
          <li key={finding.finding_id}>
            <p>{reasons[finding.reason_code]}</p>
            <p className="break-all text-xs">Source: {finding.source_selector}</p>
            {finding.target_ref && <p className="break-all text-xs">Reference: {finding.target_ref}</p>}
            <p className="text-xs">{finding.correction_surface === 'card_scenario_links'
              ? 'Review this Card’s scenario links and link an existing scenario in its Spec.'
              : 'Review the test scenarios in the Spec and correct the duplicate ID or link.'}</p>
          </li>
        ))}
      </ul>
      {context.truncated && <p className="mt-2 text-xs">Showing a bounded selection. Review the remaining scenario links in the Card and Spec.</p>}
    </section>
  );
}
