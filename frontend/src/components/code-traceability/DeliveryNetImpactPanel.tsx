import type { DeliveryNetImpact } from '@/types/delivery-evidence';

const reasons: Record<string, string> = {
  source_unknown: 'Identify the source of this declaration.',
  revision_unknown: 'Provide the base and result revisions for these changes.',
  unchanged_revision_with_delta: 'The declared change has the same base and result revision.',
  revision_chain_ambiguous: 'The declared revisions do not form one unambiguous sequence.',
  same_revision_conflicting_claims: 'Declarations disagree about the same change.',
  same_revision_path_overlap: 'File operations overlap within the same revision.',
  path_recreated_or_conflicting: 'Clarify the result of recreating this path.',
  path_no_longer_present: 'A later operation refers to a removed or renamed path.',
  rename_destination_conflict: 'The destination of a rename conflicts with another path.',
  symbol_lifecycle_ambiguous: 'Clarify the resulting symbol change.',
  test_lifecycle_ambiguous: 'Clarify the resulting test change.',
  surface_lifecycle_unknown: 'Confirm which previously affected surfaces remain in the result.',
  artifact_scope_changed: 'Reconcile symbols or tests affected by a file rename or deletion.',
  net_impact_limit: 'The combined declaration exceeds the impact limits.',
  impact_population_limit: 'More than 200 declarations require a narrower consolidation.',
  net_impact_payload_limit: 'The combined declaration is too large to display safely.',
};

export function DeliveryNetImpactPanel({ value }: { value: DeliveryNetImpact }) {
  const changes = value.sources.flatMap(source => [
    ...source.impact_evidence.files.map(row => `${source.source_ref} · ${row.repo}:${row.path} · ${row.change_kind}${row.previous_path ? ` from ${row.previous_path}` : ''}`),
    ...source.impact_evidence.symbols.map(row => `${source.source_ref} · ${row.repo}:${row.file} · ${row.name} · ${row.action}`),
    ...source.impact_evidence.tests.map(row => `${source.source_ref} · ${row.repo}:${row.test_file_path} · ${row.action}`),
    ...source.impact_evidence.surfaces.map(row => `${source.source_ref} · ${row.kind}: ${row.identifier}`),
  ]);
  return <section aria-label="Accumulated impact claims" className="space-y-2 rounded border p-3 text-sm">
    <h4 className="font-medium">Accumulated impact claims</h4>
    <p>{value.history_count} active declarations. This view does not verify changes or approve delivery.</p>
    {value.status === 'empty' && <p>No incremental impact declarations.</p>}
    {value.status === 'needs_reconciliation' && <p role="status">Needs reconciliation: {value.issue_count} unresolved source groups or limits.</p>}
    {value.status === 'composed' && !changes.length && <p>No net changes in the declared sequence. The original work history is retained.</p>}
    {value.sources.map(source => <p key={source.source_ref}>{source.source_ref}: {source.base_revision.slice(0, 12)} → {source.result_revision.slice(0, 12)}</p>)}
    <ul>{changes.slice(0, 20).map((change, index) => <li key={index}>{change}</li>)}</ul>
    {changes.length > 20 && <p>Showing 20 of {changes.length} net changes.</p>}
    <ul>{value.issues.map((issue, index) => <li key={index}>{issue.source_ref ? `${issue.source_ref}: ` : ''}{reasons[issue.code] ?? 'This declaration requires reconciliation.'}</li>)}</ul>
    {value.issues_truncated && <p>Showing {value.issues.length} of {value.issue_count} reconciliation items.</p>}
  </section>;
}
