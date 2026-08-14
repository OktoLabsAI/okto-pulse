import { useCallback, useEffect, useMemo, useState } from 'react';
import { AlertTriangle, Eye, Grid3X3 } from 'lucide-react';
import { ContextualHelpLink } from '@/components/help';
import { useDashboardApi } from '@/services/api';
import type {
  CodeTraceabilityEvidence,
  CodeTraceabilityEvidenceLink,
  CodeTraceabilityProjection,
} from '@/types';
import { ReceiptDetailModal } from './ReceiptDetailModal';
import {
  TraceabilityBadge,
  TraceabilityCurrentnessBadge,
  TraceabilityEmptyState,
} from './TraceabilityDisclosure';
import { projectedReceiptCurrentness } from './traceabilityCurrentness';

interface Props {
  boardId: string;
  subjectId: string;
  subjectVersion: number;
  boardSkipCoverage?: boolean;
  skipCoverage?: boolean;
  canEditCoverageFlags?: boolean;
  onSkipCoverageChange?: (skip: boolean) => Promise<void> | void;
}

const ENTITY_COLUMNS = [
  { label: 'FR', kinds: ['functional_requirement'] },
  { label: 'TR', kinds: ['technical_requirement'] },
  { label: 'BR', kinds: ['business_rule'] },
  { label: 'AC', kinds: ['acceptance_criterion'] },
  { label: 'API', kinds: ['api_contract'] },
  { label: 'IR', kinds: ['integration_requirement'] },
  { label: 'OR', kinds: ['observability_requirement'] },
  { label: 'Decision', kinds: ['decision'] },
  { label: 'Test', kinds: ['test_scenario'] },
] as const;

function shortId(value: string) {
  return value.length > 18 ? `${value.slice(0, 14)}…` : value;
}

function EntityLinks({ links }: { links: CodeTraceabilityEvidenceLink[] }) {
  if (links.length === 0) return <span className="text-gray-300 dark:text-gray-600">—</span>;
  return (
    <div className="flex min-w-[3rem] flex-wrap justify-center gap-1">
      {links.map((link) => (
        <span
          key={link.id}
          title={`${link.entity_type}:${link.entity_id} · ${link.relation_type}`}
          className="rounded bg-violet-50 px-1.5 py-0.5 font-mono text-[9px] text-violet-700 dark:bg-violet-950/40 dark:text-violet-300"
        >
          {shortId(link.entity_id)}
        </span>
      ))}
    </div>
  );
}

function MatrixRow({
  evidence,
  projection,
  onViewReceipt,
}: {
  evidence: CodeTraceabilityEvidence;
  projection: CodeTraceabilityProjection;
  onViewReceipt: (receiptId: string) => void;
}) {
  const links = projection.links.filter((link) => link.evidence_id === evidence.id);
  const disposition = projection.dispositions.find(
    (candidate) => candidate.evidence_id === evidence.id && candidate.active,
  );
  const currentness = projectedReceiptCurrentness(
    projection,
    evidence.investigation_receipt_id,
  );
  const state = links.length > 0 ? 'linked' : disposition?.disposition || 'pending';

  return (
    <tr className="border-t border-gray-100 align-top dark:border-gray-700/70">
      <th scope="row" className="sticky left-0 z-[1] min-w-[16rem] bg-white px-3 py-3 text-left dark:bg-gray-800">
        <div className="flex items-start gap-2">
          <Grid3X3 size={13} className="mt-0.5 shrink-0 text-gray-400" />
          <div className="min-w-0">
            <p className="line-clamp-2 text-xs font-medium text-gray-800 dark:text-gray-100">
              {evidence.claim || 'Agent-submitted code evidence'}
            </p>
            <p className="mt-1 truncate font-mono text-[10px] font-normal text-gray-400" title={evidence.relative_path || evidence.id}>
              {evidence.relative_path || shortId(evidence.id)}
            </p>
          </div>
        </div>
      </th>
      {ENTITY_COLUMNS.map((column) => (
        <td key={column.label} className="px-2 py-3 text-center">
          <EntityLinks links={links.filter((link) => (column.kinds as readonly string[]).includes(link.entity_type))} />
        </td>
      ))}
      <td className="min-w-[9rem] px-3 py-3">
        <div className="flex flex-col items-start gap-1">
          <TraceabilityBadge kind="receipt-accepted" />
          <TraceabilityCurrentnessBadge currentness={currentness} />
          <button
            type="button"
            onClick={() => onViewReceipt(evidence.investigation_receipt_id)}
            className="mt-0.5 inline-flex items-center gap-1 text-[10px] font-medium text-blue-600 hover:underline dark:text-blue-400"
          >
            <Eye size={10} /> View receipt
          </button>
        </div>
      </td>
      <td className="min-w-[8rem] px-3 py-3">
        <span className={`inline-flex rounded-full px-2 py-0.5 text-[10px] font-semibold ${
          state === 'pending'
            ? 'bg-amber-50 text-amber-700 dark:bg-amber-950/30 dark:text-amber-300'
            : 'bg-emerald-50 text-emerald-700 dark:bg-emerald-950/30 dark:text-emerald-300'
        }`}>
          {state.replace(/_/g, ' ')}
        </span>
      </td>
    </tr>
  );
}

export function EvidenceMatrixPanel({
  boardId,
  subjectId,
  subjectVersion,
  boardSkipCoverage = false,
  skipCoverage = false,
  canEditCoverageFlags = false,
  onSkipCoverageChange,
}: Props) {
  const api = useDashboardApi();
  const [projection, setProjection] = useState<CodeTraceabilityProjection | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [receiptId, setReceiptId] = useState<string | null>(null);
  const [updatingSkipCoverage, setUpdatingSkipCoverage] = useState(false);

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError(null);
    try {
      setProjection(await api.getCodeTraceabilityProjection(
        boardId,
        'spec',
        subjectId,
        subjectVersion,
        {
          profile: 'full',
          signal,
          contextScope: 'gate',
        },
      ));
    } catch (caught) {
      if (!signal?.aborted) {
        setError(caught instanceof Error ? caught.message : 'Could not load the Code Evidence Matrix.');
      }
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [api, boardId, subjectId, subjectVersion]);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [boardSkipCoverage, load, skipCoverage]);

  const evidence = useMemo(() => {
    if (!projection) return [];
    const inheritedIds = new Set(projection.inherited_evidence_ids);
    return projection.evidence.filter((item) => inheritedIds.has(item.id));
  }, [projection]);
  const coverage = useMemo(() => {
    if (!projection) return null;
    const { total, linked, dispositioned, pending, coverage_pct: coveragePct } = projection.coverage;
    return {
      total,
      addressed: linked + dispositioned,
      pending,
      coveragePct,
    };
  }, [projection]);
  const projectionIncomplete = projection !== null && (
    projection.profile !== 'full'
    || projection.context_scope !== 'gate'
    || projection.gate_readiness.blockers.some(
      (blocker) => blocker.code === 'code_traceability_projection_incomplete',
    )
  );
  const projectionSkipCoverage = projection?.coverage.skipped
    || projection?.gate_readiness.evidence_coverage_skipped
    || false;

  const coverageStatus = useMemo(() => {
    if (projectionIncomplete) {
      return {
        label: 'Incomplete',
        className: 'bg-red-100 text-red-700 dark:bg-red-950/40 dark:text-red-300',
      };
    }
    if (boardSkipCoverage) {
      return {
        label: 'Skipped by Board',
        className: 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
      };
    }
    if (skipCoverage) {
      return {
        label: 'Skipped for this Spec',
        className: 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
      };
    }
    if (projectionSkipCoverage) {
      return {
        label: 'Skipped',
        className: 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
      };
    }
    if (!coverage || coverage.total === 0) {
      return {
        label: 'No evidence',
        className: 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300',
      };
    }
    if (coverage.pending === 0) {
      return {
        label: 'Covered',
        className: 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300',
      };
    }
    return {
      label: 'Pending',
      className: 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
    };
  }, [boardSkipCoverage, coverage, projectionIncomplete, projectionSkipCoverage, skipCoverage]);

  const toggleSkipCoverage = useCallback(async () => {
    if (!onSkipCoverageChange || updatingSkipCoverage) return;
    setUpdatingSkipCoverage(true);
    try {
      await onSkipCoverageChange(!skipCoverage);
    } finally {
      setUpdatingSkipCoverage(false);
    }
  }, [onSkipCoverageChange, skipCoverage, updatingSkipCoverage]);

  return (
    <div className="space-y-4" data-testid="spec-evidence-matrix-panel">
      <div>
        <h2 className="text-sm font-semibold text-gray-900 dark:text-white">Code evidence matrix</h2>
        <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
          Maps inherited Code Evidence from the parent Refinement to the structured Spec entities it supports.
        </p>
        <ContextualHelpLink
          sectionId="code-traceability"
          ariaLabel="Learn how the Code Evidence Matrix works"
          testId="code-evidence-matrix-help-link"
          className="mt-1 text-[11px]"
        >
          Why this is a matrix
        </ContextualHelpLink>
      </div>

      {coverage && (
        <section
          aria-label="Code Evidence coverage"
          className="rounded-lg border border-gray-200 p-3 dark:border-gray-700"
        >
          <div className="mb-2 flex items-center justify-between gap-3">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
              Code Evidence coverage
            </h3>
            <span
              data-testid="code-evidence-coverage-status"
              className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${coverageStatus.className}`}
            >
              {coverageStatus.label}
            </span>
          </div>
          <div className="mb-3 h-2 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-700">
            <div
              role="progressbar"
              aria-label="Code Evidence coverage progress"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={Math.round(coverage.coveragePct)}
              aria-valuetext={projectionIncomplete ? 'Coverage projection incomplete' : undefined}
              className={`h-full rounded-full transition-all duration-500 ${
                projectionIncomplete
                  ? 'bg-red-500'
                  : coverage.pending === 0 && coverage.total > 0
                    ? 'bg-green-500'
                    : 'bg-amber-500'
              }`}
              style={{ width: `${coverage.coveragePct}%` }}
            />
          </div>
          <div className="grid gap-2 sm:grid-cols-3">
            <div className="rounded-lg border border-gray-200 px-3 py-2 dark:border-gray-700">
              <p className="text-lg font-semibold text-gray-900 dark:text-white">
                {coverage.addressed}/{coverage.total}
              </p>
              <p className="text-[11px] text-gray-400">evidence items addressed</p>
            </div>
            <div className="rounded-lg border border-gray-200 px-3 py-2 dark:border-gray-700">
              <p className="text-lg font-semibold text-amber-600 dark:text-amber-400">{coverage.pending}</p>
              <p className="text-[11px] text-gray-400">evidence item{coverage.pending === 1 ? '' : 's'} pending</p>
            </div>
            <div className="rounded-lg border border-gray-200 px-3 py-2 dark:border-gray-700">
              <p className="text-lg font-semibold text-blue-600 dark:text-blue-400">{Math.round(coverage.coveragePct)}%</p>
              <p className="text-[11px] text-gray-400">coverage</p>
            </div>
          </div>
        </section>
      )}

      {projectionIncomplete && (
        <div
          role="alert"
          className="flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300"
        >
          <AlertTriangle size={14} className="mt-0.5 shrink-0" />
          <span>
            Coverage could not be evaluated completely. Validation remains blocked, and the coverage skip does not bypass this technical condition.
          </span>
        </div>
      )}

      {boardSkipCoverage && (
        <div
          data-testid="code-evidence-board-skip-notice"
          className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-200"
        >
          <AlertTriangle size={14} className="mt-0.5 shrink-0" />
          <span>
            Code Evidence Matrix coverage is skipped for every Spec by the Board setting.
            Configure it in Menu &gt; Board &gt; Coverage Overrides. Incomplete projections and
            independently applicable technical gates remain enforced; Evidence and receipts
            are not altered.
          </span>
        </div>
      )}

      {canEditCoverageFlags && onSkipCoverageChange && (
        <div className="flex items-center justify-between gap-4 rounded-lg border border-gray-200 bg-gray-50/50 px-3 py-2 dark:border-gray-700 dark:bg-gray-700/20">
          <div>
            <span className="text-xs font-medium text-gray-700 dark:text-gray-300">
              Skip Code Evidence coverage for this Spec
            </span>
            <p className="text-[10px] text-gray-400">
              {boardSkipCoverage
                ? 'The Board-wide skip currently governs. This local setting is stored independently and will apply if the Board skip is removed.'
                : 'Bypass only pending links or dispositions. The underlying Evidence and receipt status remain unchanged.'}
            </p>
          </div>
          <button
            type="button"
            role="switch"
            aria-label="Skip Code Evidence coverage"
            aria-checked={skipCoverage}
            aria-busy={updatingSkipCoverage}
            disabled={updatingSkipCoverage}
            onClick={() => void toggleSkipCoverage()}
            className={`relative h-5 w-10 shrink-0 rounded-full transition-colors disabled:cursor-wait disabled:opacity-60 ${
              skipCoverage ? 'bg-amber-500' : 'bg-gray-300 dark:bg-gray-600'
            }`}
          >
            <span
              className={`absolute left-0.5 top-0.5 h-4 w-4 rounded-full bg-white transition-transform ${
                skipCoverage ? 'translate-x-5' : ''
              }`}
            />
          </button>
        </div>
      )}

      {loading && <div className="py-10 text-center text-xs text-gray-400" role="status">Loading accepted evidence links…</div>}
      {error && (
        <div className="flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300" role="alert">
          <AlertTriangle size={14} className="mt-0.5 shrink-0" />
          <span>{error}</span>
        </div>
      )}
      {!loading && !error && projection && evidence.length === 0 && (
        <TraceabilityEmptyState noun="inherited code evidence" />
      )}
      {!loading && !error && projection && evidence.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-700">
          <table className="w-full border-collapse text-xs">
            <thead className="bg-gray-50 text-[10px] font-semibold uppercase tracking-wide text-gray-400 dark:bg-gray-900/50">
              <tr>
                <th className="sticky left-0 z-[2] min-w-[16rem] bg-gray-50 px-3 py-2 text-left dark:bg-gray-900">Evidence</th>
                {ENTITY_COLUMNS.map((column) => <th key={column.label} className="px-2 py-2 text-center">{column.label}</th>)}
                <th className="px-3 py-2 text-left">Receipt</th>
                <th className="px-3 py-2 text-left">Disposition</th>
              </tr>
            </thead>
            <tbody>
              {evidence.map((item) => (
                <MatrixRow key={item.id} evidence={item} projection={projection} onViewReceipt={setReceiptId} />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {receiptId && (
        <ReceiptDetailModal
          boardId={boardId}
          receiptId={receiptId}
          onClose={() => setReceiptId(null)}
          onRevoked={() => void load()}
        />
      )}
    </div>
  );
}
