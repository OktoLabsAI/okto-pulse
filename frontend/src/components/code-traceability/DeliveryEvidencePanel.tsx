import { resolveDeliveryGateMode } from '@/components/board/deliveryGateSettings';
import { DeliveryReadinessPanel } from './DeliveryReadinessPanel';
import { DeliveryExceptionDetails } from './DeliveryExceptionDetails';
import type { ReadinessSpecLocation } from './readinessNavigation';
import { useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { PulseLoader } from '@/components/shared/PulseLoader';
import { useDashboardApi } from '@/services/api';
import { ObligationRefText } from './obligationPresentation';
import type { DeliveryEvidenceProjection } from '@/types/delivery-evidence';
import { isPersistedReadinessProjection, type PersistedReadinessProjection, type ReadinessMetric } from '@/types/delivery-readiness';
import { decisionStatusLabel } from '@/components/specs/DecisionReviewSection';
import { readinessReadMode } from '@/lib/readinessMode';

interface Props {
  boardId: string;
  specId: string;
  revision?: string;
  skipDeliveryEvidence?: boolean;
  onSkipDeliveryEvidenceChange?: (value: boolean) => void;
  onOpenDecisions?: () => void;
  onOpenReadinessSpec?: (location: ReadinessSpecLocation) => void;
  focusExceptionId?: string | null;
}

function metricLabel(metric?: ReadinessMetric) {
  if (!metric || metric.status === 'unknown') return 'Unknown';
  if (metric.status === 'not_applicable') return 'N/A';
  return `${metric.percent}%`;
}

function unitLabel(state: string, phase: 'implementation' | 'verification') {
  const labels: Record<string, string> = {
    satisfied: phase === 'implementation' ? '✓ Current proof' : '✓ Verified',
    missing: '◌ Pending', partial: '◐ Partial', failed: '✕ Failed',
    stale: '↻ Stale', conflict: '⚠ Conflict', waived: 'Waived · no proof credit',
    not_applicable: 'N/A', unknown: 'Unknown',
  };
  return labels[state] ?? 'Unknown';
}

function ObligationEvidenceDetails({ data, row, currentRow }: {
  data: DeliveryEvidenceProjection; row: DeliveryEvidenceProjection['rows'][number];
  currentRow?: NonNullable<DeliveryEvidenceProjection['current_proof_rows']>[number];
}) {
  const implementationIds = currentRow?.implementation_ids ?? row.implementation_ids;
  const ids = new Set([...implementationIds, ...row.test_ids,
    ...row.implementation_waiver_ids, ...row.test_waiver_ids]);
  return <div className="space-y-4 break-words">
    <section aria-label="Implementation evidence" className="space-y-2">
      <h4 className="font-semibold">Implementation evidence</h4>
      {implementationIds.length === 0 && <p>{currentRow ? 'No current implementation proof.' : 'Current implementation proof unavailable.'}</p>}
      {implementationIds.map(id => {
        const proof = data.implementations.find(item => item.id === id);
        return <div key={id} className="space-y-1 rounded border border-gray-200 p-2 dark:border-gray-700">
          <p className="break-all">Record: {id}</p>
          {proof ? <>
            <p>Card: {proof.card_id}</p><p>Implementation author: {proof.actor_id || 'Not available'}</p><p>{proof.explanation}</p>
            {proof.executions.map(execution => <p key={execution.execution_id} className="break-all">
              {execution.relative_path} · {execution.source_ref} @ {execution.result_revision} · {execution.symbol || execution.execution_id}
              {' · '}{execution.current_accepted_execution ? 'Current accepted execution' : 'Not a current accepted execution'}
            </p>)}
          </> : <p>Record details unavailable in this projection.</p>}
        </div>;
      })}
      {row.implementation_waiver_ids.length > 0 && <p className="break-all">Implementation waivers (not proof): {row.implementation_waiver_ids.join(', ')}</p>}
    </section>
    <section aria-label="Verification evidence" className="space-y-2">
      <h4 className="font-semibold">Verification evidence</h4>
      {row.test_ids.length === 0 && <p>No admitted verification evidence.</p>}
      {row.test_ids.map(id => {
        const proof = data.tests?.find(item => item.id === id);
        return <div key={id} className="space-y-1 rounded border border-gray-200 p-2 dark:border-gray-700">
          <p className="break-all">Record: {id}</p>
          {proof ? <><p>Card: {proof.card_id} · Scenario: {proof.scenario_id}</p>
            <p>Verification author: {proof.actor_id || 'Not available'}</p>
            <p>Result: {proof.result} · {proof.current_verified_run ? 'Current verified run' : 'Current verification not established'}</p></>
            : <p>Record details unavailable in this projection.</p>}
        </div>;
      })}
      {row.test_waiver_ids.length > 0 && <p className="break-all">Verification waivers (not proof): {row.test_waiver_ids.join(', ')}</p>}
    </section>
    {data.records.filter(record => ids.has(record.id)).map(record => <p key={record.id} className="break-all">
      {record.id} · {record.created_at} · Actor: {record.actor_id}{record.revoked ? ' · Revoked' : ''}
      {' · '}{record.payload.justification}
    </p>)}
  </div>;
}

// Spec "Delivery" tab — informational rollup over the linked cards' ledgers
// (mockup sm_5ebd7063). Read-only by design: recording is card-scoped
// (task DoD) and waivers are rollup-level, human-only — this surface only
// presents the aggregated verdict, never mutates it.
export function DeliveryEvidencePanel({ boardId, specId, revision, skipDeliveryEvidence = false, onSkipDeliveryEvidenceChange, onOpenDecisions, onOpenReadinessSpec, focusExceptionId }: Props) {
  const api = useDashboardApi();
  const legacyMode = readinessReadMode() === 'legacy';
  const [storedData, setData] = useState<DeliveryEvidenceProjection | null>(null);
  const [dataOwner, setDataOwner] = useState<{ api: typeof api; key: string }>();
  const [gateMode, setGateMode] = useState<'advisory' | 'blocking' | null>(null);
  const [error, setError] = useState('');
  const [reload, setReload] = useState(0);
  const [storedProjection, setProjection] = useState<PersistedReadinessProjection | null>(null);
  const [projectionOwner, setProjectionOwner] = useState<{ api: typeof api; key: string }>();
  const [projectionError, setProjectionError] = useState('');
  const [showDetails, setShowDetails] = useState(legacyMode);
  const scopeKey = JSON.stringify([boardId, specId, revision, reload]);
  const data = dataOwner?.api === api && dataOwner.key === scopeKey ? storedData : null;
  const projection = projectionOwner?.api === api && projectionOwner.key === scopeKey ? storedProjection : null;

  useEffect(() => {
    if (legacyMode) return;
    const refresh = () => setReload(value => value + 1);
    const visible = () => { if (document.visibilityState === 'visible') refresh(); };
    window.addEventListener('pulse:delivery-evidence-changed', refresh);
    window.addEventListener('focus', refresh);
    document.addEventListener('visibilitychange', visible);
    return () => {
      window.removeEventListener('pulse:delivery-evidence-changed', refresh);
      window.removeEventListener('focus', refresh);
      document.removeEventListener('visibilitychange', visible);
    };
  }, [legacyMode]);

  useEffect(() => {
    const controller = new AbortController();
    setProjection(null); setProjectionError('');
    if (legacyMode) return () => controller.abort();
    let retry = 1000;
    void (async () => {
      while (!controller.signal.aborted) {
        if (document.visibilityState !== 'visible') {
          await new Promise(resolve => setTimeout(resolve, 500));
          continue;
        }
        try {
          let page = await api.getPersistedReadinessProjection(boardId, specId, { unitLimit: 250 }, controller.signal);
          if (!isPersistedReadinessProjection(page) || page.scope.board_id !== boardId || page.scope.spec_id !== specId)
            throw new Error('Readiness projection scope mismatch.');
          const rows = [...page.units];
          const seen = new Set<string>();
          while (page.page.has_more) {
            const cursor = page.page.next_cursor;
            if (!cursor || seen.has(cursor)) throw new Error('Readiness page cursor invalid.');
            seen.add(cursor);
            const next = await api.getPersistedReadinessProjection(boardId, specId,
              { unitLimit: 250, cursor }, controller.signal);
            if (!isPersistedReadinessProjection(next) || next.projection.generation_id !== page.projection.generation_id
              || next.scope.spec_edition !== page.scope.spec_edition) throw new Error('Readiness generation changed. Restart this view.');
            rows.push(...next.units);
            page = next;
          }
          if (!controller.signal.aborted) {
            setProjectionOwner({ api, key: scopeKey });
            setProjection({ ...page, units: rows });
          }
          setProjectionError(''); retry = 1000;
          if (!['updating', 'pending_initial'].includes(page.projection.freshness)) return;
          await new Promise(resolve => setTimeout(resolve, 500));
        } catch (failure) {
          if (controller.signal.aborted) return;
          setProjectionError(failure instanceof Error ? failure.message : 'Readiness projection unavailable.');
          await new Promise(resolve => setTimeout(resolve, retry));
          retry = Math.min(5000, retry * 2);
        }
      }
    })();
    return () => controller.abort();
  }, [api, boardId, specId, revision, reload, scopeKey, legacyMode]);

  useEffect(() => {
    const controller = new AbortController();
    setData(null); setGateMode(null); setError('');
    if (showDetails || focusExceptionId) api.getDeliveryEvidence(boardId, specId, controller.signal).then(value => {
      if (value.board_id !== boardId || value.spec_id !== specId) throw new Error('Delivery evidence scope changed. Refresh the Spec.');
      if (!controller.signal.aborted) {
        setDataOwner({ api, key: scopeKey });
        setData(value);
      }
    }).catch(err => { if (!controller.signal.aborted) setError(err instanceof Error ? err.message : 'Delivery proof could not be loaded.'); });
    api.getBoard(boardId).then(board => {
      if (!controller.signal.aborted) setGateMode(resolveDeliveryGateMode(board.settings?.delivery_evidence_gate));
    }).catch(() => { if (!controller.signal.aborted) setGateMode(null); });
    return () => controller.abort();
  }, [api, boardId, specId, revision, reload, focusExceptionId, showDetails, scopeKey]);

  const missingImplementation = data?.rows.filter(row => !row.implementation_satisfied).length ?? 0;
  const currentProofRows = new Map((data?.current_proof_rows ?? []).map(row =>
    [`${row.obligation_ref}:${row.semantic_sha256}`, row]));
  const missingTest = data?.rows.filter(row => !row.test_satisfied && !row.decision_verification_status).length ?? 0;
  const pendingDecisions = data?.rows.filter(row => row.decision_verification_status && !row.test_satisfied).length ?? 0;
  const summary = !data ? '' : data.allowed
    ? `All ${data.rows.length} obligations satisfied (proof or authorized waiver)`
    : [missingImplementation ? `implementation missing on ${missingImplementation}` : '',
       missingTest ? `test coverage missing on ${missingTest}` : '',
       pendingDecisions ? `${pendingDecisions} decision verification(s) pending` : '']
      .filter(Boolean).join(' · ') + ' obligation' + ((missingImplementation + missingTest) === 1 ? '' : 's');

  return <section className="space-y-5" aria-label="Implementation and verification">
    {focusExceptionId && data?.board_id === boardId && data.spec_id === specId
      && <DeliveryExceptionDetails data={data} recordId={focusExceptionId} />}
    {projection && <DeliveryReadinessPanel boardId={boardId} specId={specId} revision={`${revision ?? ''}:${reload}`}
      persistedValue={projection} onOpenReadinessSpec={onOpenReadinessSpec} />}
    {legacyMode && <DeliveryReadinessPanel boardId={boardId} specId={specId}
      revision={`${revision ?? ''}:${reload}`} onOpenReadinessSpec={onOpenReadinessSpec} />}
    {!legacyMode && !projection && !projectionError && <PulseLoader label="Loading readiness projection…" size="sm" className="py-4" />}
    {projectionError && <p role="alert" className="text-xs text-amber-700">{projectionError}</p>}
    <p className="text-sm text-gray-500 dark:text-gray-400">Track implementation and verification for each obligation. Expand an item to inspect its evidence.</p>
    <div className="flex flex-wrap items-center justify-between gap-4">
      <div className="flex min-w-0 items-center gap-2">
        {data && (data.allowed
          ? <span className="shrink-0 rounded-full bg-emerald-100 px-2 py-0.5 text-[10px] uppercase tracking-wide text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300">Complete</span>
          : <span className="shrink-0 rounded-full bg-red-100 px-2 py-0.5 text-[10px] uppercase tracking-wide text-red-700 dark:bg-red-900/40 dark:text-red-300">Blocked</span>)}
        {(error || data) && <span className="text-xs text-gray-500 dark:text-gray-400" role="status">
          {error || `${summary} · Edition ${data!.edition}`}
        </span>}
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[10px] uppercase tracking-wide text-gray-600 dark:bg-gray-800 dark:text-gray-300" data-testid="delivery-gate-mode">
          Gate: {gateMode === null ? 'Unknown' : gateMode === 'advisory' ? 'Advisory' : 'Blocking'} (Board)
        </span>
        <button type="button" onClick={() => setReload(v => v + 1)} aria-label="Refresh delivery rollup"
          className="inline-flex h-9 shrink-0 items-center justify-center gap-2 rounded-lg border border-gray-200 bg-white px-3 text-xs font-medium text-gray-700 shadow-sm transition-colors hover:bg-gray-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700">
          <RefreshCw size={14} /> Refresh
        </button>
      </div>
    </div>

      {showDetails && !data && !error && <PulseLoader label="Loading delivery details…" size="sm" className="py-6" />}
      <div className="flex items-center justify-between gap-3 rounded-lg border border-gray-200 bg-gray-50/50 px-3 py-2 dark:border-gray-700 dark:bg-gray-700/20">
        <div className="min-w-0">
          <span className="text-xs font-medium text-gray-700 dark:text-gray-300">Skip delivery evidence requirement</span>
          <p className="text-[10px] text-gray-400">Allow moving spec to Done without complete delivery proof — the coverage verdict stays visible. Waivers (per obligation) remain human-only.</p>
        </div>
        {onSkipDeliveryEvidenceChange ? (
          <button
            type="button"
            role="switch"
            aria-checked={skipDeliveryEvidence}
            aria-label="Skip delivery evidence requirement"
            data-testid="delivery-skip-toggle"
            onClick={() => onSkipDeliveryEvidenceChange(!skipDeliveryEvidence)}
            className={`relative h-5 w-10 shrink-0 rounded-full transition-colors ${skipDeliveryEvidence ? 'bg-amber-500' : 'bg-gray-300 dark:bg-gray-600'}`}
          >
            <span className={`absolute left-0.5 top-0.5 h-4 w-4 rounded-full bg-white transition-transform ${skipDeliveryEvidence ? 'translate-x-5' : ''}`} />
          </button>
        ) : (
          <span className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] uppercase tracking-wide ${skipDeliveryEvidence ? 'bg-amber-100 text-amber-700' : 'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400'}`}>
            {skipDeliveryEvidence ? 'Skipped' : 'Active'}
          </span>
        )}
      </div>

    {data && data.status === 'done' && !data.allowed && (
      <p className="rounded-md border border-gray-200 bg-gray-50 px-3 py-2 text-xs text-gray-500 dark:border-gray-800 dark:bg-gray-900/40 dark:text-gray-400">
        This completed Spec has not been reopened. Missing proof may have been recorded retrospectively or waived by an authorized human.
      </p>
    )}

    {!legacyMode && <div>
      <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-400 dark:text-gray-500">Coverage by obligation</h3>
      {projection && <p className="mb-2 text-xs text-gray-500" role="status">
        {projection.projection.freshness === 'current' ? 'Current' : projection.projection.freshness === 'updating'
          ? 'Updating · previous coherent generation' : projection.projection.freshness === 'pending_initial'
            ? 'Waiting for calculation' : 'Calculation unavailable'}
        {projection.projection.generation_id ? ` · Generation ${projection.projection.generation_id}` : ''}
        {' · '}Implementation {metricLabel(projection.summaries.find(row => row.card_id === null)?.implementation)}
        {' · '}Verification {metricLabel(projection.summaries.find(row => row.card_id === null)?.verification)}
      </p>}
      {projection?.projection.generation_id && <div className="overflow-x-auto"><table className="w-full text-sm" data-testid="delivery-coverage-table">
        <thead><tr className="border-b text-left text-xs text-gray-500"><th scope="col">Obligation</th>
          <th scope="col">Implementation</th><th scope="col">Verification</th></tr></thead>
        <tbody>{projection.units.filter(unit => unit.card_id === null && unit.kind === 'obligation').map(unit =>
          <tr key={`${unit.ref}:${unit.semantic_sha256}`} className="border-b border-gray-100 dark:border-gray-800">
            <td className="py-2 pr-4"><ObligationRefText value={unit.ref} /></td>
            <td className="py-2" title={unit.implementation_reason_codes.join(', ')}>{unitLabel(unit.implementation_state, 'implementation')}</td>
            <td className="py-2" title={unit.verification_reason_codes.join(', ')}>{unitLabel(unit.verification_state, 'verification')}</td>
          </tr>)}</tbody>
      </table></div>}
    </div>}
    <button type="button" className="btn btn-secondary text-xs" onClick={() => setShowDetails(value => !value)}>
      {showDetails ? 'Hide detailed evidence' : 'Show detailed evidence'}
    </button>

    {showDetails && data && <>
      <div>
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-400 dark:text-gray-500">Detailed evidence by obligation</h3>
        <p className="mb-2 text-xs text-gray-500 dark:text-gray-400">Implementation marks use the same current proof assessment as the percentage above. Delivery qualification also requires its contributing Cards to reach Done.</p>
        <div className="overflow-x-auto">
          <table className="w-full text-sm" data-testid="delivery-detailed-coverage-table">
            <thead>
              <tr className="border-b border-gray-100 text-left text-xs text-gray-400 dark:border-gray-800">
                <th scope="col" className="py-1 pr-4">Obligation</th>
                <th scope="col" className="px-2">Implementation</th>
                <th scope="col" className="px-2">Verification</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-50 dark:divide-gray-800">
              {data.rows.map(row => {
                const ref = row.obligation.binding.obligation_ref;
                const waivedImpl = row.implementation_waiver_ids.length > 0;
                const waivedTest = row.test_waiver_ids.length > 0;
                const decision = !!row.decision_verification_status;
                const currentProof = currentProofRows.get(`${ref}:${row.obligation.binding.semantic_sha256}`);
                return <tr key={ref}>
                  <td className="min-w-0 py-1.5 pr-4 text-gray-700 dark:text-gray-200">
                    <details className="group py-2">
                    <summary className="cursor-pointer rounded font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500">{row.obligation.title}</summary>
                    <div className="mt-3 space-y-3 rounded-lg bg-gray-50 p-3 text-xs dark:bg-gray-800/50">
                    <ObligationRefText value={ref} />
                    {!decision && row.required_card_ids && <p className="text-xs">{row.required_card_ids.length} planned implementation Card(s)</p>}
                    {Boolean(row.missing_card_ids?.length) && <p className="text-xs text-amber-600 dark:text-amber-400">Pending implementation Cards: {row.missing_card_ids!.slice(0, 20).join(', ')}{row.missing_card_ids!.length > 20 ? ' (additional Cards omitted)' : ''}</p>}
                    {row.missing_criteria?.slice(0, 20).map(([implementation, criterion]) => <p key={JSON.stringify([implementation, criterion])} className="text-xs text-amber-600 dark:text-amber-400">Implementation {implementation}: missing verification of {criterion}</p>)}
                    {(row.missing_criteria?.length ?? 0) > 20 && <p className="text-xs">Additional criterion gaps are omitted from this summary.</p>}
                    <p className="break-all text-gray-500">Semantic revision: {row.obligation.binding.semantic_sha256}</p>
                    {decision ? <div className="space-y-2"><p>{decisionStatusLabel(row.decision_verification_status!)}</p>
                      <p>Adherence reuses selected obligation evidence and any required inspection. No separate implementation Card is required.</p>
                      {onOpenDecisions && <button type="button" className="btn btn-secondary text-xs" onClick={onOpenDecisions}>Review in Decisions</button>}
                    </div> : <ObligationEvidenceDetails data={data} row={row} currentRow={currentProof} />}
                    </div>
                    </details>
                  </td>
                  <td className="px-2 text-xs" title={decision ? 'Decisions do not require their own implementation' : 'Current proof and delivery qualification are separate stages'}>
                    {decision ? <span className="text-gray-500">Not applicable</span> : <div className="space-y-1">
                      <div className={currentProof?.implementation_satisfied ? 'text-green-600 dark:text-green-400' : 'text-amber-600 dark:text-amber-400'}>
                        {currentProof ? currentProof.implementation_satisfied ? '✓ Current proof' : '◌ Current proof missing' : 'Current proof unavailable'}
                      </div>
                      <div className={row.implementation_satisfied && !waivedImpl ? 'text-green-600 dark:text-green-400' : 'text-amber-600 dark:text-amber-400'}>
                        {waivedImpl ? 'Waived' : row.implementation_satisfied ? <><span>✓</span> Delivery qualified</> : <><span>◌</span> Delivery pending</>}
                      </div>
                    </div>}
                  </td>
                  <td className="px-2 text-green-600 dark:text-green-400" title={waivedTest ? 'Explicitly waived — human authorization' : row.test_satisfied ? 'Current passing evidence for this obligation' : 'Missing / stale'}>
                    {decision ? <span className="text-xs">{decisionStatusLabel(row.decision_verification_status!)}</span> : waivedTest ? <span className="text-xs text-amber-600 dark:text-amber-400">Waived</span> : row.test_satisfied ? '✓' : <span className="text-amber-500">◌</span>}
                  </td>
                </tr>;
              })}
            </tbody>
          </table>
        </div>
      </div>

      {data.per_card && data.per_card.length > 0 && (
        <div>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-400 dark:text-gray-500">Per card</h3>
          <div className="space-y-2">
            {data.per_card.map(card => {
              const proven = card.obligations.filter(o => o.implementation_satisfied).length;
              const isTest = card.card_type === 'test';
              return <div key={card.card_id} className="flex items-center justify-between rounded-md border border-gray-200 p-3 dark:border-gray-800" data-testid={`delivery-card-${card.card_id}`}>
                <div className="min-w-0">
                  <div className="truncate text-sm font-medium text-gray-700 dark:text-gray-200">{card.title}</div>
                  <div className="text-xs text-gray-400 dark:text-gray-500">
                    {isTest
                      ? 'Test card · records authenticated outcomes; delivery requires current passing evidence for every required criterion'
                      : card.obligations.length === 0
                        ? 'No derived obligations'
                        : proven === card.obligations.length
                          ? `${proven} obligation${proven === 1 ? '' : 's'} with delivery-qualified proof`
                          : `${proven}/${card.obligations.length} obligations with delivery-qualified proof · ${card.obligations.length - proven} pending qualification`}
                  </div>
                </div>
                <span className={`shrink-0 text-xs font-medium ${isTest ? 'text-gray-400 dark:text-gray-500' : card.satisfied ? 'text-green-600 dark:text-green-400' : 'text-amber-600 dark:text-amber-400'}`}>
                  {isTest ? 'Verification gate' : card.satisfied ? 'Implementation proven' : 'Implementation pending'}
                </span>
              </div>;
            })}
          </div>
        </div>
      )}

      <p className="text-xs text-gray-400 dark:text-gray-500">
        Waivers are recorded at rollup level and require human authorization. Proof is recorded on each task or test card.
      </p>
    </>}
  </section>;
}
