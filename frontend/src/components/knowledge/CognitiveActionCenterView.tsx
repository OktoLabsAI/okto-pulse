/**
 * Cognitive Action Center — operational view over the cognitive readiness
 * read-model (S3.3 / card 974f5146, spec 2731a346; mockup sm_35b21529).
 *
 * Read-only projection + the central skip/clear write-path. The UI NEVER
 * recomputes precedence or enforcement:
 *  - readiness_effect / precedence_explanation / blocking come from the backend;
 *  - "would block done" language is shown ONLY when would_block_done is true
 *    (enforcement active AND a gate-blocking tier) — blocking alone is readiness
 *    language, not gate-enforcement language;
 *  - technical blockers (DLQ / open canonical debt) are surfaced as technical
 *    and NEVER offer skip/no_action (the backend also rejects with 409);
 *  - terminal history is informational/non-blocking;
 *  - tasks/tests without reusable cognition are advisory/non-blocking;
 *  - a cognitive reason_code is visually separated from a technical error_cause;
 *  - justification / evidence_refs are audit detail, never bounded labels.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle,
  Brain,
  CheckCircle2,
  Clock,
  RefreshCw,
  Search,
  ShieldAlert,
  X,
} from 'lucide-react';

import {
  clearCognitiveSkip,
  getReadinessItems,
  getReadinessMetrics,
  recordCognitiveSkip,
  ReadinessActionError,
} from '@/services/cognitive-readiness-api';
import {
  isRevisitRequiredReason,
  isTechnicalBlocker,
  REVISIT_REQUIRED_REASON_CODES,
  SELECTABLE_REASON_CODES,
  TERMINAL_REASON_CODES,
  type CognitiveReadinessItem,
  type CognitiveReadinessListResponse,
  type CognitiveReadinessMetrics,
  type ReadinessEffect,
  type ReadinessSignalFilter,
} from '@/types/cognitive-readiness';

interface CognitiveActionCenterViewProps {
  boardId: string;
  onClose: () => void;
}

const SIGNAL_FILTERS: { id: ReadinessSignalFilter; label: string }[] = [
  { id: 'all', label: 'All signals' },
  { id: 'cognitive_pending', label: 'Cognitive pending' },
  { id: 'skipped', label: 'Skipped' },
  { id: 'revisit_required', label: 'Revisit-required' },
  { id: 'open_canonical_debt', label: 'Open canonical debt' },
  { id: 'terminal_history', label: 'Terminal history' },
  { id: 'dlq', label: 'DLQ' },
];

// Bounded readiness_effect → presentation. "blocks done" wording is reserved
// for the would_block_done flag, NOT derived from blocking here.
const EFFECT_META: Record<ReadinessEffect, { label: string; tone: string }> = {
  blocking_technical: {
    label: 'Technical blocker',
    tone: 'bg-rose-100 text-rose-700 dark:bg-rose-900/40 dark:text-rose-300',
  },
  blocking_cognitive: {
    label: 'Cognitive pending',
    tone: 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
  },
  blocking_revisit_lapsed: {
    label: 'Revisit lapsed',
    tone: 'bg-orange-100 text-orange-700 dark:bg-orange-900/40 dark:text-orange-300',
  },
  ready_skip: {
    label: 'Skip valid',
    tone: 'bg-sky-100 text-sky-700 dark:bg-sky-900/40 dark:text-sky-300',
  },
  ready_committed: {
    label: 'Committed',
    tone: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300',
  },
  advisory: {
    label: 'Advisory',
    tone: 'bg-slate-100 text-slate-600 dark:bg-slate-800/60 dark:text-slate-300',
  },
  ready: {
    label: 'Ready',
    tone: 'bg-slate-100 text-slate-600 dark:bg-slate-800/60 dark:text-slate-300',
  },
};

function effectMeta(effect: string) {
  return (
    EFFECT_META[effect as ReadinessEffect] ?? {
      label: effect,
      tone: 'bg-slate-100 text-slate-600 dark:bg-slate-800/60 dark:text-slate-300',
    }
  );
}

export function CognitiveActionCenterView({
  boardId,
  onClose,
}: CognitiveActionCenterViewProps) {
  const [data, setData] = useState<CognitiveReadinessListResponse | null>(null);
  const [metrics, setMetrics] = useState<CognitiveReadinessMetrics | null>(null);
  const [signal, setSignal] = useState<ReadinessSignalFilter>('all');
  const [search, setSearch] = useState('');
  const [activeSearch, setActiveSearch] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchAll = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [items, m] = await Promise.all([
        getReadinessItems(boardId, {
          signal,
          search: activeSearch || undefined,
          limit: 200,
        }),
        getReadinessMetrics(boardId),
      ]);
      setData(items);
      setMetrics(m);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load readiness');
    } finally {
      setLoading(false);
    }
  }, [boardId, signal, activeSearch]);

  useEffect(() => {
    void fetchAll();
  }, [fetchAll]);

  const enforcementActive = data?.summary.enforcement_active ?? false;

  return (
    <div
      className="flex flex-col h-full w-full bg-gray-50 dark:bg-gray-900"
      data-testid="cognitive-action-center"
    >
      {/* Header */}
      <div className="px-6 pt-5 pb-3 border-b border-gray-200 dark:border-gray-800 flex items-center justify-between bg-white dark:bg-gray-900">
        <div>
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white inline-flex items-center gap-2">
            <Brain className="w-5 h-5 text-violet-500" />
            Cognitive Action Center
          </h2>
          <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
            Cognitive readiness signals, technical blockers and bounded metrics.{' '}
            <span data-testid="cac-enforcement">
              {enforcementActive
                ? 'Done-gate enforcement is ACTIVE for this board.'
                : 'Advisory only — done-gate enforcement is off for this board.'}
            </span>
          </p>
        </div>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={fetchAll}
            disabled={loading}
            className="p-1.5 text-gray-400 hover:text-gray-600 hover:bg-gray-100 dark:hover:bg-white/10 rounded-lg disabled:opacity-50"
            title="Refresh"
            data-testid="cac-refresh"
            aria-label="Refresh readiness"
          >
            <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          </button>
          <button
            type="button"
            onClick={onClose}
            className="p-1.5 text-gray-400 hover:text-gray-600 hover:bg-gray-100 dark:hover:bg-white/10 rounded-lg"
            aria-label="Close action center"
          >
            <X className="w-5 h-5" />
          </button>
        </div>
      </div>

      {/* Counters */}
      <CounterRow metrics={metrics} />

      {/* Filters + search */}
      <div className="px-6 py-3 flex flex-wrap items-center gap-2 border-b border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900">
        {SIGNAL_FILTERS.map((f) => (
          <button
            key={f.id}
            type="button"
            onClick={() => setSignal(f.id)}
            data-testid={`cac-filter-${f.id}`}
            className={`px-2.5 py-1 rounded-full text-xs font-medium border ${
              signal === f.id
                ? 'bg-violet-600 text-white border-violet-600'
                : 'bg-white dark:bg-gray-800 text-gray-600 dark:text-gray-300 border-gray-200 dark:border-gray-700 hover:border-violet-400'
            }`}
          >
            {f.label}
          </button>
        ))}
        <form
          className="ml-auto relative"
          onSubmit={(e) => {
            e.preventDefault();
            setActiveSearch(search.trim());
          }}
        >
          <Search className="w-3.5 h-3.5 absolute left-2 top-1/2 -translate-y-1/2 text-gray-400" />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="artifact_id / source_ref / reason_code"
            data-testid="cac-search"
            className="pl-7 pr-2 py-1 text-xs rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-200 w-72"
          />
        </form>
      </div>

      {/* Body */}
      <div className="flex-1 overflow-auto px-6 py-4">
        {loading && !data && <p className="text-sm text-gray-500">Loading…</p>}

        {error && (
          <div className="py-8 text-center" data-testid="cac-error">
            <p className="text-sm text-rose-600 dark:text-rose-400 mb-3">{error}</p>
            <button
              type="button"
              onClick={fetchAll}
              className="px-3 py-1.5 text-xs rounded-lg bg-violet-600 hover:bg-violet-700 text-white"
            >
              Retry
            </button>
          </div>
        )}

        {!loading && !error && data && data.items.length === 0 && (
          <div className="py-12 text-center" data-testid="cac-empty-state">
            <div className="inline-flex items-center justify-center w-14 h-14 rounded-full bg-emerald-100 dark:bg-emerald-900/30 mb-3">
              <CheckCircle2 className="w-7 h-7 text-emerald-600 dark:text-emerald-400" />
            </div>
            <h3 className="text-sm font-medium text-gray-900 dark:text-white mb-1">
              No readiness signals
            </h3>
            <p className="text-xs text-gray-500 dark:text-gray-400">
              Nothing pending, skipped, or blocking for this filter.
            </p>
          </div>
        )}

        {!error && data && data.items.length > 0 && (
          <table className="w-full text-xs" data-testid="cac-table">
            <thead className="bg-gray-50 dark:bg-gray-800 sticky top-0">
              <tr className="text-left text-gray-500 dark:text-gray-400 uppercase tracking-wide">
                <th className="px-3 py-2 font-medium">Artifact</th>
                <th className="px-3 py-2 font-medium">Signal</th>
                <th className="px-3 py-2 font-medium">Status</th>
                <th className="px-3 py-2 font-medium">Cognitive reason</th>
                <th className="px-3 py-2 font-medium">Technical cause</th>
                <th className="px-3 py-2 font-medium">Readiness</th>
                <th className="px-3 py-2 font-medium">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200 dark:divide-gray-800">
              {data.items.map((item, idx) => (
                <ReadinessRow
                  key={`${item.artifact_id}:${item.signal_source}:${idx}`}
                  item={item}
                  boardId={boardId}
                  onChanged={fetchAll}
                />
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Bounded metrics panel */}
      <MetricsPanel metrics={metrics} />
    </div>
  );
}

function CounterRow({ metrics }: { metrics: CognitiveReadinessMetrics | null }) {
  const tiles = [
    {
      key: 'cognitive_pending',
      label: 'Cognitive pending',
      value: metrics?.cognitive_pending_signals ?? 0,
      icon: <Brain className="w-4 h-4" />,
      tone: 'bg-amber-50 text-amber-800 dark:bg-amber-900/30 dark:text-amber-200',
    },
    {
      key: 'expired_revisit',
      label: 'Revisit expired',
      value: metrics?.expired_revisit_skips ?? 0,
      icon: <Clock className="w-4 h-4" />,
      tone: 'bg-orange-50 text-orange-800 dark:bg-orange-900/30 dark:text-orange-200',
    },
    {
      key: 'open_canonical_debt',
      label: 'Open canonical debt',
      value: metrics?.open_canonical_debt ?? 0,
      icon: <ShieldAlert className="w-4 h-4" />,
      tone: 'bg-rose-50 text-rose-800 dark:bg-rose-900/30 dark:text-rose-200',
    },
    {
      key: 'dlq',
      label: 'Technical DLQ',
      value: metrics?.technical_dlq ?? 0,
      icon: <AlertTriangle className="w-4 h-4" />,
      tone: 'bg-rose-50 text-rose-800 dark:bg-rose-900/30 dark:text-rose-200',
    },
    {
      key: 'terminal_history',
      label: 'Terminal history',
      value: metrics?.terminal_history ?? 0,
      icon: <CheckCircle2 className="w-4 h-4" />,
      tone: 'bg-emerald-50 text-emerald-800 dark:bg-emerald-900/30 dark:text-emerald-200',
    },
  ];
  return (
    <div className="px-6 py-3 grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3 bg-gray-50 dark:bg-gray-900/40">
      {tiles.map((t) => (
        <div
          key={t.key}
          data-testid={`cac-counter-${t.key}`}
          className={`rounded-lg p-3 ${t.tone}`}
        >
          <div className="flex items-center gap-1.5 mb-1">{t.icon}</div>
          <div className="text-xs font-medium">{t.label}</div>
          <div className="text-xl font-bold">{t.value}</div>
        </div>
      ))}
    </div>
  );
}

interface ReadinessRowProps {
  item: CognitiveReadinessItem;
  boardId: string;
  onChanged: () => void;
}

function ReadinessRow({ item, boardId, onChanged }: ReadinessRowProps) {
  const [skipping, setSkipping] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const technical = isTechnicalBlocker(item);
  const meta = effectMeta(item.readiness_effect);
  const aliasExtra = item.aliases.filter((a) => a !== item.source_ref_original);

  const onClear = async () => {
    setBusy(true);
    setActionError(null);
    try {
      await clearCognitiveSkip(boardId, item.source_ref_original);
      onChanged();
    } catch (err) {
      setActionError(_actionMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const onSkipSubmit = async (reasonCode: string, justification: string, revisitAt: string) => {
    setBusy(true);
    setActionError(null);
    try {
      await recordCognitiveSkip(boardId, {
        sourceRef: item.source_ref_original,
        reasonCode,
        justification: justification || undefined,
        revisitAt: revisitAt || undefined,
      });
      setSkipping(false);
      onChanged();
    } catch (err) {
      setActionError(_actionMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <tr className="hover:bg-gray-50 dark:hover:bg-gray-800/50 align-top">
        <td className="px-3 py-2">
          <div
            className="font-mono text-gray-900 dark:text-white truncate max-w-[200px]"
            title={item.source_ref_original}
          >
            {item.artifact_id}
          </div>
          <div className="text-[10px] text-gray-400">{item.artifact_type}</div>
          {aliasExtra.length > 0 && (
            <div
              className="text-[10px] text-gray-400"
              data-testid="cac-aliases"
              title={item.aliases.join(', ')}
            >
              aka {aliasExtra.join(', ')}
            </div>
          )}
        </td>
        <td className="px-3 py-2">
          <span className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-300">
            {item.signal}
          </span>
          <div className="text-[10px] text-gray-400 mt-0.5">{item.signal_source}</div>
        </td>
        <td className="px-3 py-2 text-gray-700 dark:text-gray-300">
          {item.status ?? '—'}
          {item.outcome_type && (
            <div className="text-[10px] text-gray-400">{item.outcome_type}</div>
          )}
        </td>
        {/* Cognitive reason — distinct from technical cause */}
        <td className="px-3 py-2" data-testid="cac-reason-code">
          {item.reason_code ? (
            <span className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium bg-violet-100 text-violet-700 dark:bg-violet-900/40 dark:text-violet-300">
              {item.reason_code}
            </span>
          ) : (
            <span className="text-gray-300 dark:text-gray-600">—</span>
          )}
          {item.revisit_at && (
            <div className="text-[10px] text-gray-400">revisit: {item.revisit_at}</div>
          )}
        </td>
        {/* Technical error_cause — never a selectable reason */}
        <td className="px-3 py-2" data-testid="cac-error-cause">
          {item.error_cause ? (
            <span className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium bg-rose-100 text-rose-700 dark:bg-rose-900/40 dark:text-rose-300">
              {item.error_cause}
            </span>
          ) : (
            <span className="text-gray-300 dark:text-gray-600">—</span>
          )}
        </td>
        <td className="px-3 py-2">
          <span
            className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium ${meta.tone}`}
            title={JSON.stringify(item.precedence_explanation)}
          >
            {meta.label}
          </span>
          {item.would_block_done && (
            <div
              className="text-[10px] font-semibold text-rose-600 dark:text-rose-400 mt-0.5"
              data-testid="cac-would-block-done"
            >
              WOULD BLOCK DONE
            </div>
          )}
        </td>
        <td className="px-3 py-2">
          {technical ? (
            <span
              className="text-[10px] text-rose-500"
              data-testid="cac-technical-no-skip"
              title="Technical blocker — resolve/reprocess it; not skippable as a cognitive reason."
            >
              Resolve technical
            </span>
          ) : item.status === 'skipped' ? (
            <button
              type="button"
              onClick={onClear}
              disabled={busy}
              data-testid="cac-clear"
              className="px-2 py-0.5 text-[10px] rounded border border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-50"
            >
              Clear / reopen
            </button>
          ) : (
            <button
              type="button"
              onClick={() => setSkipping((s) => !s)}
              disabled={busy}
              data-testid="cac-skip-toggle"
              className="px-2 py-0.5 text-[10px] rounded border border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-50"
            >
              Skip…
            </button>
          )}
        </td>
      </tr>
      {(skipping || actionError) && (
        <tr className="bg-gray-50 dark:bg-gray-800/30">
          <td colSpan={7} className="px-4 py-3">
            {actionError && (
              <p
                className="text-[11px] text-rose-600 dark:text-rose-400 mb-2"
                data-testid="cac-action-error"
              >
                {actionError}
              </p>
            )}
            {skipping && !technical && (
              <SkipForm busy={busy} onCancel={() => setSkipping(false)} onSubmit={onSkipSubmit} />
            )}
          </td>
        </tr>
      )}
    </>
  );
}

interface SkipFormProps {
  busy: boolean;
  onCancel: () => void;
  onSubmit: (reasonCode: string, justification: string, revisitAt: string) => void;
}

function SkipForm({ busy, onCancel, onSubmit }: SkipFormProps) {
  const [reasonCode, setReasonCode] = useState<string>(SELECTABLE_REASON_CODES[0]);
  const [justification, setJustification] = useState('');
  const [revisitAt, setRevisitAt] = useState('');
  const needsRevisit = isRevisitRequiredReason(reasonCode);

  return (
    <form
      data-testid="cac-skip-form"
      className="flex flex-wrap items-end gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit(reasonCode, justification, revisitAt);
      }}
    >
      <label className="text-[10px] text-gray-500 dark:text-gray-400">
        Reason
        <select
          value={reasonCode}
          onChange={(e) => setReasonCode(e.target.value)}
          data-testid="cac-skip-reason"
          className="block mt-0.5 text-xs rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 px-1.5 py-1"
        >
          <optgroup label="Terminal">
            {TERMINAL_REASON_CODES.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </optgroup>
          <optgroup label="Revisit-required">
            {REVISIT_REQUIRED_REASON_CODES.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </optgroup>
        </select>
      </label>
      {needsRevisit && (
        <label className="text-[10px] text-gray-500 dark:text-gray-400">
          Revisit at (ISO)
          <input
            value={revisitAt}
            onChange={(e) => setRevisitAt(e.target.value)}
            data-testid="cac-skip-revisit"
            placeholder="2026-12-31T00:00:00Z"
            className="block mt-0.5 text-xs rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 px-1.5 py-1 w-52"
          />
        </label>
      )}
      <label className="text-[10px] text-gray-500 dark:text-gray-400 flex-1 min-w-[160px]">
        Justification (audit)
        <input
          value={justification}
          onChange={(e) => setJustification(e.target.value)}
          data-testid="cac-skip-justification"
          className="block mt-0.5 text-xs rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 px-1.5 py-1 w-full"
        />
      </label>
      <button
        type="submit"
        disabled={busy}
        data-testid="cac-skip-confirm"
        className="px-2.5 py-1 text-[11px] rounded bg-violet-600 hover:bg-violet-700 text-white disabled:opacity-50"
      >
        Confirm skip
      </button>
      <button
        type="button"
        onClick={onCancel}
        className="px-2.5 py-1 text-[11px] rounded border border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-300"
      >
        Cancel
      </button>
    </form>
  );
}

function MetricsPanel({ metrics }: { metrics: CognitiveReadinessMetrics | null }) {
  const groups = useMemo(() => {
    if (!metrics) return [];
    return [
      { key: 'readiness_effect', label: 'By readiness', data: metrics.by_readiness_effect },
      { key: 'status', label: 'By status', data: metrics.by_status },
      { key: 'reason_code', label: 'By reason (bounded)', data: metrics.by_reason_code },
      { key: 'age', label: 'By age', data: metrics.by_age_bucket },
    ];
  }, [metrics]);

  if (!metrics) return null;
  return (
    <div
      className="px-6 py-3 border-t border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900"
      data-testid="cac-metrics-panel"
    >
      <div className="text-[10px] uppercase tracking-wide text-gray-400 mb-2">
        Bounded readiness metrics ({metrics.total} signals)
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-3">
        {groups.map((g) => (
          <div key={g.key} data-testid={`cac-metric-${g.key}`}>
            <div className="text-[10px] font-medium text-gray-500 dark:text-gray-400 mb-1">
              {g.label}
            </div>
            <div className="flex flex-wrap gap-1">
              {Object.entries(g.data).length === 0 ? (
                <span className="text-[10px] text-gray-300 dark:text-gray-600">—</span>
              ) : (
                Object.entries(g.data).map(([label, count]) => (
                  <span
                    key={label}
                    className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300"
                  >
                    <span className="font-mono">{label}</span>
                    <span className="font-semibold">{count}</span>
                  </span>
                ))
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function _actionMessage(err: unknown): string {
  if (err instanceof ReadinessActionError) {
    if (err.status === 409) {
      return `${err.message} (technical blocker — resolve it, don't skip).`;
    }
    return err.message;
  }
  return err instanceof Error ? err.message : 'Action failed';
}
