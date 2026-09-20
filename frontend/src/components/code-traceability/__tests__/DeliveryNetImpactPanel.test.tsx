import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import type { DeliveryNetImpact } from '@/types/delivery-evidence';
import { DeliveryNetImpactPanel } from '../DeliveryNetImpactPanel';

afterEach(cleanup);
function value(): DeliveryNetImpact {
  return { contract_version: 'delivery-net-impact/v1', status: 'composed', claim_only: true, history_count: 2,
    sources: [{ source_ref: 'repository', base_revision: 'a'.repeat(40), result_revision: 'c'.repeat(40), record_ids: ['one', 'two'],
      impact_evidence: { schema_version: 1, files: [], symbols: [], surfaces: [], tests: [], evidence_refs: [] } }],
    issues: [], issue_count: 0, issues_truncated: false };
}
it('distinguishes cancelled net changes from the retained work history', () => {
  render(<DeliveryNetImpactPanel value={value()} />);
  expect(screen.getByText(/No net changes in the declared sequence/)).toBeInTheDocument();
  expect(screen.getByText(/2 active declarations/)).toHaveTextContent('does not verify changes or approve delivery');
});
it('shows a bounded unresolved population without implying completion', () => {
  render(<DeliveryNetImpactPanel value={{ ...value(), status: 'needs_reconciliation', sources: [], issue_count: 30, issues_truncated: true,
    issues: [{ source_ref: 'repository', code: 'revision_chain_ambiguous', record_ids: ['one'] }] }} />);
  expect(screen.getByRole('status')).toHaveTextContent('Needs reconciliation: 30');
  expect(screen.getByText(/do not form one unambiguous sequence/)).toBeInTheDocument();
  expect(screen.getByText('Showing 1 of 30 reconciliation items.')).toBeInTheDocument();
  expect(screen.queryByText(/No net changes/)).not.toBeInTheDocument();
});
it('caps displayed changes and shows rename origin with source identity', () => {
  const projection = value();
  projection.sources[0].impact_evidence.files = Array.from({ length: 21 }, (_, i) => ({ repo: 'core', path: `new${i}.py`, previous_path: `old${i}.py`, change_kind: 'renamed' }));
  render(<DeliveryNetImpactPanel value={projection} />);
  expect(screen.getByText('repository · core:new0.py · renamed from old0.py')).toBeInTheDocument();
  expect(screen.queryByText(/core:new20.py/)).not.toBeInTheDocument();
  expect(screen.getByText('Showing 20 of 21 net changes.')).toBeInTheDocument();
});
