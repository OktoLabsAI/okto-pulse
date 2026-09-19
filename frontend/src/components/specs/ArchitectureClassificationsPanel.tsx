import { useEffect, useState, type ReactNode } from 'react';
import { useDashboardApi } from '@/services/api';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import type { ArchitectureClassificationReviewItem, ArchitectureClassificationsResponse, ArchitectureReviewState } from '@/types/architecture-classifications';
import { ArchitectureClassificationAuthoring, type ArchitectureAuthoringAuthority } from './ArchitectureClassificationAuthoring';

const labels: Record<ArchitectureReviewState, string> = {
  pending: 'Pending', current: 'Current', review_required: 'Review required',
  unresolved: 'Unresolved', retired: 'Retired', unavailable: 'Unavailable',
};
const dispositions = { promote_to_ir: 'Promoted to IR', associate_existing_ir: 'Associated with IR', context_only: 'Context only' };

function failure(error: unknown) {
  if (error instanceof AuthenticatedFetchError && [401, 403].includes(error.status)) return 'Permission to read classifications is no longer available.';
  if (error instanceof AuthenticatedFetchError && error.status === 409) return 'The classification or source changed. Refresh the review before continuing.';
  return 'Classification review could not be loaded. Its completeness is unknown.';
}

function ReviewDetails({ boardId, specId, specVersion, specEdition, item, children }: {
  boardId: string; specId: string; specVersion: number; specEdition: number; item: ArchitectureClassificationReviewItem;
  children?: ReactNode;
}) {
  const api = useDashboardApi();
  const [digest, setDigest] = useState<string | null>(null);
  const [result, setResult] = useState<{ digest: string; detail?: ArchitectureClassificationReviewItem; error?: string } | null>(null);
  useEffect(() => {
    if (!digest) { setResult(null); return; }
    const controller = new AbortController();
    let active = true;
    void (async () => {
      try {
        const response = await api.getArchitectureClassifications(boardId, specId, controller.signal, { candidateId: item.candidate_id, sourceDigest: digest });
        const detail = response.items.find(row => row.candidate_id === item.candidate_id);
        if (response.board_id !== boardId || response.spec_id !== specId || response.spec_edition !== specEdition || response.spec_version !== specVersion || response.profile !== 'detail' || !detail || !('current_contract' in detail)
          || !(detail.source_digests.includes(digest) || (!detail.source_variant_count && detail.analyzed_source_digest === digest))) throw new Error('scope changed');
        if (active) setResult({ digest, detail });
      } catch (error) {
        if (active) setResult({ digest, error: failure(error) });
      }
    })();
    return () => { active = false; controller.abort(); };
  }, [api, boardId, specId, specVersion, specEdition, item.candidate_id, digest]);
  const current = result?.digest === digest ? result : null;
  const variants = item.source_digests.length ? item.source_digests : item.analyzed_source_digest ? [item.analyzed_source_digest] : [];
  return <article className="mt-2 rounded border border-gray-200 p-2 dark:border-gray-700">
    {children}
    <p className="text-sm font-medium">{item.name || item.interface_id} · {labels[item.state]}</p>
    <p className="text-xs">Origin: {item.root_design_id} · Interface: {item.interface_id}</p>
    {item.state === 'retired' && <p className="text-xs">The source left the current population. Existing IR obligations remain in scope.</p>}
    {item.state === 'unresolved' && <p className="text-xs">Resolve the source or history conflict before classifying this contract.</p>}
    {item.issues.includes('architecture_classification_ir_not_active_in_spec') && <p className="text-xs">A referenced IR is missing, inactive or ambiguous in this Spec.</p>}
    {variants.map((value, index) => <button key={value} type="button" className="mr-3 text-xs text-blue-600 dark:text-blue-400" aria-expanded={digest === value} onClick={() => setDigest(digest === value ? null : value)}>
      {variants.length > 1 ? `Review variant ${index + 1}` : 'Review decision and contract'}
    </button>)}
    {item.source_digests_truncated && <p className="text-xs">Additional conflicting variants exist. This contract is unresolved.</p>}
    {digest && <div className="mt-2 text-xs">
      {!current && <p role="status">Loading classification details…</p>}
      {current?.error && <p role="alert">{current.error}</p>}
      {current?.detail && <>
        {current.detail.changed_paths?.length ? <p>Changed paths: {current.detail.changed_paths.join(', ')}</p> : null}
        {current.detail.changed_paths_truncated && <p>The change list is truncated. Review the full contracts below.</p>}
        {current.detail.remainder_state && <p>Remaining context: {labels[current.detail.remainder_state]}</p>}
        {current.detail.decisions?.map(record => <div key={record.decision_id} className="mt-2">
          <p>{dispositions[record.disposition]} · {labels[record.state]}</p>
          <p>By {record.actor_id} at {record.classified_at} · Spec version {record.spec_version}</p>
          <p>Scope: {record.scope_paths.map(path => path || 'Whole contract').join(', ')}</p>
          {record.integration_requirement_refs.length > 0 && <p>IRs: {record.integration_requirement_refs.join(', ')}</p>}
          {record.reason && <p>Reason: {record.reason}</p>}
          {record.remainder_reason && <p>Remaining context reason: {record.remainder_reason}</p>}
          <p>Analyzed revisions: {record.adopted_sources.map(source => `${source.design_id} v${source.revision}`).join(', ')}</p>
        </div>)}
        {(['analyzed_contract', 'current_contract'] as const).map(key => current.detail![key] && <div key={key} className="mt-2">
          <p>{key === 'analyzed_contract' ? 'Analyzed contract' : 'Current contract'}</p>
          <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-all">{JSON.stringify(current.detail![key], null, 2)}</pre>
        </div>)}
      </>}
    </div>}
  </article>;
}

export function ArchitectureClassificationsPanel({ boardId, specId, specVersion, canRead, authoring }: {
  boardId: string; specId: string; specVersion: number; canRead: boolean;
  authoring?: ArchitectureAuthoringAuthority;
}) {
  const api = useDashboardApi();
  const [open, setOpen] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [filter, setFilter] = useState<ArchitectureReviewState | ''>('');
  const baseScope = JSON.stringify([boardId, specId, specVersion, canRead, open, refresh, filter]);
  const [page, setPage] = useState({ scope: baseScope, offset: 0 });
  const offset = page.scope === baseScope ? page.offset : 0;
  const scope = JSON.stringify([baseScope, offset]);
  const [result, setResult] = useState<{ scope: string; data?: ArchitectureClassificationsResponse; error?: string } | null>(null);
  useEffect(() => {
    if (!canRead || !open) { setResult(null); return; }
    const controller = new AbortController();
    let active = true;
    void (async () => {
      try {
        const data = await api.getArchitectureClassifications(boardId, specId, controller.signal, { offset, limit: 25, ...(filter ? { state: filter } : {}) });
        if (data.board_id !== boardId || data.spec_id !== specId || data.profile !== 'summary') throw new Error('scope mismatch');
        if (active) setResult({ scope, data });
      } catch (error) { if (active) setResult({ scope, error: failure(error) }); }
    })();
    return () => { active = false; controller.abort(); };
  }, [api, boardId, specId, scope, offset, filter, canRead, open]);
  const current = result?.scope === scope ? result : null;
  const data = current?.data;
  return <section aria-label="Architecture classifications" className="mb-4 rounded border border-gray-200 p-3 dark:border-gray-700">
    <h3 className="text-sm font-medium">Architecture classifications</h3>
    {!canRead ? <p className="mt-2 text-sm">Spec, architecture and IR read permissions are required.</p> : <>
      <button type="button" aria-expanded={open} className="mt-2 text-xs text-blue-600 dark:text-blue-400" onClick={() => { setOpen(!open); setRefresh(value => value + 1); }}>Review classifications</button>
      {open && <>
        <p className="mt-2 text-xs">This review does not approve requirements or authorize starting the Spec. Context decisions do not waive existing IRs.</p>
        <button type="button" className="mr-3 text-xs text-blue-600 dark:text-blue-400" onClick={() => setRefresh(value => value + 1)}>Refresh classifications</button>
        <label className="text-xs">Classification state <select value={filter} onChange={event => setFilter(event.target.value as ArchitectureReviewState | '')}>
          <option value="">All states</option>{Object.entries(labels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select></label>
        {!current && <p role="status">Loading classification review…</p>}
        {current?.error && <p role="alert">{current.error}</p>}
        {data && <>
          {(!data.enumeration_complete || data.total === null) && <p role="alert">Sources or history are incomplete. Totals are unknown; counts cover observed contracts only.</p>}
          {data.enumeration_complete && !data.classification_complete && <p>Classification is incomplete across the adopted architecture.</p>}
          {data.classification_complete && <p>Current contracts are classified. Execution gates and semantic review still apply.</p>}
          <ul aria-label="Global classification counts" className="mt-2 text-xs">{Object.entries(labels).map(([value, label]) => <li key={value}>{label}: {data.state_counts[value as ArchitectureReviewState]}</li>)}</ul>
          {data.items.length === 0 && <p>No classifications in this page or filter.</p>}
          {!authoring?.canClassify && data.items.map(item => <ReviewDetails key={`${scope}:${item.candidate_id}`} boardId={boardId} specId={specId} specVersion={data.spec_version} specEdition={data.spec_edition} item={item} />)}
          {(offset > 0 || data.has_more) && <nav aria-label="Classification pages" className="mt-2 flex gap-3 text-xs">
            <button type="button" disabled={offset === 0} onClick={() => setPage({ scope: baseScope, offset: Math.max(0, offset - 25) })}>Previous classifications</button>
            <button type="button" disabled={!data.has_more} onClick={() => setPage({ scope: baseScope, offset: offset + 25 })}>Next classifications</button>
          </nav>}
        </>}
        {authoring?.canClassify && <ArchitectureClassificationAuthoring
          key={JSON.stringify([boardId, specId, specVersion, authoring.specEdition, authoring.canPromote, authoring.canAssociate])}
          {...authoring} boardId={boardId} specId={specId} specVersion={specVersion}
          sourceReady={Boolean(data?.source_complete && data.spec_version === specVersion && data.spec_edition === authoring.specEdition)}
          items={data?.items ?? []}
          renderItem={(item, selection) => <ReviewDetails key={`${scope}:${item.candidate_id}`} boardId={boardId} specId={specId} specVersion={data!.spec_version} specEdition={data!.spec_edition} item={item}>{selection}</ReviewDetails>}
        />}
        {authoring && !authoring.canClassify && <p className="mt-2 text-xs">Classification authoring requires an unarchived Draft Spec and permission to edit its content.</p>}
      </>}
    </>}
  </section>;
}
