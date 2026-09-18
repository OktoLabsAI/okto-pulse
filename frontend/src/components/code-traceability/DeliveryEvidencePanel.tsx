import { useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { useDashboardApi } from '@/services/api';
import { ObligationRefText } from './obligationPresentation';
import type { DeliveryEvidenceProjection } from '@/types/delivery-evidence';

interface Props {
  boardId: string;
  specId: string;
  skipDeliveryEvidence?: boolean;
  onSkipDeliveryEvidenceChange?: (value: boolean) => void;
}

// Spec "Delivery" tab — informational rollup over the linked cards' ledgers
// (mockup sm_5ebd7063). Read-only by design: recording is card-scoped
// (task DoD) and waivers are rollup-level, human-only — this surface only
// presents the aggregated verdict, never mutates it.
export function DeliveryEvidencePanel({ boardId, specId, skipDeliveryEvidence = false, onSkipDeliveryEvidenceChange }: Props) {
  const api = useDashboardApi();
  const [data, setData] = useState<DeliveryEvidenceProjection | null>(null);
  const [gateMode, setGateMode] = useState<'advisory' | 'blocking' | null>(null);
  const [error, setError] = useState('');
  const [reload, setReload] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setData(null); setError('');
    api.getDeliveryEvidence(boardId, specId, controller.signal).then(value => {
      if (!controller.signal.aborted) setData(value);
    }).catch(err => { if (!controller.signal.aborted) setError(err instanceof Error ? err.message : 'Delivery proof could not be loaded.'); });
    api.getBoard(boardId).then(board => {
      if (!controller.signal.aborted) setGateMode(board.settings?.delivery_evidence_gate ?? 'blocking');
    }).catch(() => { if (!controller.signal.aborted) setGateMode('blocking'); });
    return () => controller.abort();
  }, [api, boardId, specId, reload]);

  const missingImplementation = data?.rows.filter(row => !row.implementation_satisfied).length ?? 0;
  const missingTest = data?.rows.filter(row => !row.test_satisfied).length ?? 0;
  const summary = !data ? '' : data.allowed
    ? `All ${data.rows.length} obligations satisfied by accepted proof`
    : [missingImplementation ? `implementation missing on ${missingImplementation}` : '',
       missingTest ? `test coverage missing on ${missingTest}` : '']
      .filter(Boolean).join(' · ') + ' obligation' + ((missingImplementation + missingTest) === 1 ? '' : 's');

  return <section className="space-y-5" aria-label="Delivery evidence">
    <div className="flex items-center justify-between gap-4">
      <div className="flex min-w-0 items-center gap-2">
        {data && (data.allowed
          ? <span className="shrink-0 rounded-full bg-emerald-100 px-2 py-0.5 text-[10px] uppercase tracking-wide text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300">Complete</span>
          : <span className="shrink-0 rounded-full bg-red-100 px-2 py-0.5 text-[10px] uppercase tracking-wide text-red-700 dark:bg-red-900/40 dark:text-red-300">Blocked</span>)}
        <span className="truncate text-xs text-gray-500 dark:text-gray-400" role="status">
          {error ? error : !data ? 'Loading delivery rollup…' : `${summary} · Edition ${data.edition}`}
        </span>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[10px] uppercase tracking-wide text-gray-600 dark:bg-gray-800 dark:text-gray-300" data-testid="delivery-gate-mode">
          Gate: {gateMode === 'advisory' ? 'Advisory' : 'Blocking'} (Board)
        </span>
        <button type="button" onClick={() => setReload(v => v + 1)} aria-label="Refresh delivery rollup"
          className="rounded border border-gray-200 p-1.5 text-gray-500 transition-colors hover:bg-gray-100 dark:border-gray-800 dark:text-gray-400 dark:hover:bg-gray-800">
          <RefreshCw size={12} />
        </button>
      </div>
    </div>

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

    {data && <>
      <div>
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-400 dark:text-gray-500">Coverage by obligation</h3>
        <div className="overflow-x-auto">
          <table className="w-full text-sm" data-testid="delivery-coverage-table">
            <thead>
              <tr className="border-b border-gray-100 text-left text-xs text-gray-400 dark:border-gray-800">
                <th scope="col" className="py-1 pr-4">Obligation</th>
                <th scope="col" className="px-2">Implementation</th>
                <th scope="col" className="px-2">Test</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-50 dark:divide-gray-800">
              {data.rows.map(row => {
                const ref = row.obligation.binding.obligation_ref;
                const waivedImpl = row.implementation_waiver_ids.length > 0;
                const waivedTest = row.test_waiver_ids.length > 0;
                return <tr key={ref}>
                  <td className="min-w-0 py-1.5 pr-4 text-gray-700 dark:text-gray-200">
                    <span className="block">{row.obligation.title}</span>
                    <ObligationRefText value={ref} />
                  </td>
                  <td className="px-2 text-green-600 dark:text-green-400" title={waivedImpl ? 'Explicitly waived — human authorization' : row.implementation_satisfied ? 'Accepted proof recorded' : 'No accepted proof'}>
                    {row.implementation_satisfied || waivedImpl ? '✓' : <span className="text-amber-500">◌</span>}
                  </td>
                  <td className="px-2 text-green-600 dark:text-green-400" title={waivedTest ? 'Explicitly waived — human authorization' : row.test_satisfied ? 'Verified passing run' : 'Missing / stale'}>
                    {row.test_satisfied || waivedTest ? '✓' : <span className="text-amber-500">◌</span>}
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
                      ? 'Test card · authenticates via passed scenario'
                      : card.obligations.length === 0
                        ? 'No derived obligations'
                        : proven === card.obligations.length
                          ? `${proven} obligation${proven === 1 ? '' : 's'} with accepted proof`
                          : `${proven}/${card.obligations.length} obligations with proof · ${card.obligations.length - proven} unproven`}
                  </div>
                </div>
                <span className={`shrink-0 text-xs font-medium ${isTest ? 'text-gray-400 dark:text-gray-500' : card.satisfied ? 'text-green-600 dark:text-green-400' : 'text-amber-600 dark:text-amber-400'}`}>
                  {isTest ? 'Excluded from DoD gate' : card.satisfied ? 'Satisfied' : 'In progress'}
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
