import { useEffect, useState } from 'react';
import { useDashboardApi } from '@/services/api';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import type { ArchitectureCandidatesResponse, ArchitectureContractCandidate } from '@/types/architecture-candidates';

interface Props {
  boardId: string;
  specId: string;
  specVersion: number;
  canRead: boolean;
}

const issueLabels: Record<string, string> = {
  architecture_sources_unavailable: 'The adopted sources could not be fully read.',
  architecture_contract_identity_required: 'A legacy interface needs a stable identity before it can be classified.',
  architecture_interface_identity_duplicate: 'An interface identity appears more than once in a Design.',
  architecture_contract_revision_conflict: 'Adopted copies contain conflicting versions of this contract.',
  architecture_contract_unresolved: 'A contract contains unresolved data.',
};

const signalLabels: Record<string, string> = {
  unrestricted_schema: 'Contains an unrestricted schema',
  reference_only: 'Contract declared by reference; reference has not been fetched',
  contract_content_missing: 'Contract content still needs to be described',
};

function CandidateDetails({ boardId, specId, candidate }: {
  boardId: string; specId: string; candidate: ArchitectureContractCandidate;
}) {
  const api = useDashboardApi();
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<{ contract?: Record<string, unknown>; error?: string } | null>(null);
  useEffect(() => {
    if (!open || detail) return;
    const controller = new AbortController();
    let active = true;
    void (async () => {
      try {
        const response = await api.getArchitectureCandidates(boardId, specId, controller.signal, {
          candidateId: candidate.id, sourceDigest: candidate.source_digest,
        });
        const item = response.candidates.find(value => value.id === candidate.id && value.source_digest === candidate.source_digest);
        if (response.board_id !== boardId || response.spec_id !== specId || !item?.contract) throw new Error('detail unavailable');
        if (active) setDetail({ contract: item.contract });
      } catch (error) {
        if (active) setDetail({ error: error instanceof AuthenticatedFetchError && error.status === 409
          ? 'The adopted contract changed. Refresh candidates before continuing.'
          : 'The contract could not be loaded or access is no longer available.' });
      }
    })();
    return () => { active = false; controller.abort(); };
  }, [open, detail, api, boardId, specId, candidate.id, candidate.source_digest]);
  return <div className="rounded border border-gray-200 p-2 dark:border-gray-700">
    <button type="button" aria-expanded={open} className="text-left text-sm" onClick={() => setOpen(value => !value)}>{candidate.name || candidate.interface_id}</button>
    <p className="mt-1 text-xs">{[candidate.contract_type, candidate.direction, candidate.protocol].filter(Boolean).join(' · ')}</p>
    <p className="mt-1 text-xs">Origin: {candidate.root_design_id} · Interface: {candidate.interface_id}</p>
    {candidate.signals.map(signal => <p key={signal} className="mt-1 text-xs text-amber-700 dark:text-amber-300">{signalLabels[signal] ?? 'Contract needs review'}</p>)}
    <p className="mt-1 text-xs">Adopted revisions: {candidate.adopted_sources.map(source => `${source.design_id} v${source.revision}`).join(', ')}</p>
    {open && <>
      {!detail && <p role="status" className="mt-2 text-xs">Loading contract…</p>}
      {detail?.error && <p role="alert" className="mt-2 text-xs">{detail.error}</p>}
      {detail?.contract && <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-all text-xs">{JSON.stringify(detail.contract, null, 2)}</pre>}
    </>}
  </div>;
}

export function ArchitectureCandidatesPanel({ boardId, specId, specVersion, canRead }: Props) {
  const api = useDashboardApi();
  const [refresh, setRefresh] = useState(0);
  const baseScope = JSON.stringify([boardId, specId, specVersion, refresh, canRead]);
  const [page, setPage] = useState({ scope: baseScope, offset: 0 });
  const offset = page.scope === baseScope ? page.offset : 0;
  const scope = JSON.stringify([baseScope, offset]);
  const [result, setResult] = useState<{
    scope: string;
    data?: ArchitectureCandidatesResponse;
    error?: string;
  } | null>(null);

  useEffect(() => {
    if (!canRead) return;
    const controller = new AbortController();
    let active = true;
    void (async () => {
      try {
        const data = await api.getArchitectureCandidates(boardId, specId, controller.signal, { offset, limit: 25 });
        if (data.board_id !== boardId || data.spec_id !== specId) throw new Error('scope mismatch');
        if (active) setResult({ scope, data });
      } catch (error) {
        if (!active) return;
        const denied = error instanceof AuthenticatedFetchError && [401, 403].includes(error.status);
        setResult({ scope, error: denied
          ? 'You do not have permission to read architecture candidates.'
          : 'Architecture candidates could not be loaded. The population is unknown.' });
      }
    })();
    return () => { active = false; controller.abort(); };
  }, [api, boardId, specId, scope, offset, canRead]);

  // Suppress old content immediately, even before the new effect runs.
  const current = result?.scope === scope ? result : null;
  const data = current?.data;
  return (
    <section aria-label="Architecture contract candidates" className="mb-4 rounded border border-gray-200 p-3 dark:border-gray-700">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-sm font-medium">Architecture contract candidates</h3>
        {canRead && <button type="button" className="text-xs text-blue-600 dark:text-blue-400" onClick={() => setRefresh(value => value + 1)}>Refresh candidates</button>}
      </div>
      {!canRead ? <p className="mt-2 text-sm">Architecture read permission is required.</p> : <>
        {!current && <p role="status" className="mt-2 text-sm">Loading adopted contracts…</p>}
        {current?.error && <p role="alert" className="mt-2 text-sm text-amber-700 dark:text-amber-300">{current.error}</p>}
        {data && <>
          <p className="mt-2 text-xs text-gray-600 dark:text-gray-400">Adopted contracts for Spec edition {data.spec_edition}. Reading this list does not create requirements or approve execution.</p>
          {data.population_state === 'unavailable' && <p role="alert" className="mt-2 text-sm">Sources are unavailable or incomplete. The candidate total is unknown.</p>}
          {data.population_state === 'unresolved' && <p role="alert" className="mt-2 text-sm">Some adopted contracts need resolution.</p>}
          {data.population_state === 'complete' && data.total === 0 && <p className="mt-2 text-sm">No declared contracts in the effective architecture.</p>}
          {data.issues.length > 0 && <ul className="mt-2 space-y-1 text-xs">{data.issues.map((issue, index) => <li key={index}>
            {issueLabels[issue.code] ?? 'An adopted contract could not be resolved.'}
            {issue.design_id && <span> Design: {issue.design_id}.</span>}
          </li>)}</ul>}
          {data.total !== null && data.total > 0 && <p className="mt-2 text-xs">{data.total} candidates across all adopted sources · {data.total_variants} contract variants</p>}
          {data.issues_truncated && <p className="mt-2 text-xs">Additional source issues exist. The population remains unresolved.</p>}
          <div className="mt-2 space-y-2">{data.candidates.map(candidate => <CandidateDetails key={`${scope}:${candidate.id}:${candidate.source_digest}`} boardId={boardId} specId={specId} candidate={candidate} />)}</div>
          {(offset > 0 || data.has_more) && <nav aria-label="Candidate pages" className="mt-2 flex gap-3 text-xs">
            <button type="button" disabled={offset === 0} onClick={() => setPage({ scope: baseScope, offset: Math.max(0, offset - 25) })}>Previous candidates</button>
            <button type="button" disabled={!data.has_more} onClick={() => setPage({ scope: baseScope, offset: offset + 25 })}>Next candidates</button>
          </nav>}
        </>}
      </>}
    </section>
  );
}
