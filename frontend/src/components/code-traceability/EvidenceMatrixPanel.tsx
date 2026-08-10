import { useCallback, useEffect, useMemo, useState } from 'react';
import { AlertTriangle, Eye, Grid3X3 } from 'lucide-react';
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
  TraceabilityDisclosure,
  TraceabilityEmptyState,
} from './TraceabilityDisclosure';
import { projectedReceiptCurrentness } from './traceabilityCurrentness';

interface Props {
  boardId: string;
  subjectId: string;
  subjectVersion: number;
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

export function EvidenceMatrixPanel({ boardId, subjectId, subjectVersion }: Props) {
  const api = useDashboardApi();
  const [projection, setProjection] = useState<CodeTraceabilityProjection | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [receiptId, setReceiptId] = useState<string | null>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError(null);
    try {
      setProjection(await api.getCodeTraceabilityProjection(
        boardId,
        'spec',
        subjectId,
        subjectVersion,
        'detail',
        signal,
      ));
    } catch (caught) {
      if (!signal?.aborted) {
        setError(caught instanceof Error ? caught.message : 'Could not load evidence matrix.');
      }
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [api, boardId, subjectId, subjectVersion]);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const evidence = useMemo(() => projection?.evidence ?? [], [projection]);
  const coverage = useMemo(() => {
    if (!projection) return null;

    const evidenceIds = new Set(evidence.map((item) => item.id));
    const linkedIds = new Set(
      projection.links
        .filter((link) => evidenceIds.has(link.evidence_id))
        .map((link) => link.evidence_id),
    );
    const dispositionedIds = new Set(
      projection.dispositions
        .filter((disposition) => (
          disposition.active
          && evidenceIds.has(disposition.evidence_id)
          && !linkedIds.has(disposition.evidence_id)
        ))
        .map((disposition) => disposition.evidence_id),
    );
    const total = evidence.length;
    const addressed = linkedIds.size + dispositionedIds.size;
    const pending = Math.max(total - addressed, 0);

    return {
      total,
      addressed,
      pending,
      coveragePct: total > 0 ? (addressed / total) * 100 : 0,
    };
  }, [evidence, projection]);

  return (
    <div className="space-y-4" data-testid="spec-evidence-matrix-panel">
      <div>
        <h2 className="text-sm font-semibold text-gray-900 dark:text-white">Evidence matrix</h2>
        <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
          Links from immutable Refinement evidence to structured Spec entities.
        </p>
      </div>
      <TraceabilityDisclosure />

      {coverage && (
        <div className="grid gap-2 sm:grid-cols-3">
          <div className="rounded-lg border border-gray-200 px-3 py-2 dark:border-gray-700">
            <p className="text-lg font-semibold text-gray-900 dark:text-white">
              {coverage.addressed}/{coverage.total}
            </p>
            <p className="text-[11px] text-gray-400">evidence receipts dispositioned</p>
          </div>
          <div className="rounded-lg border border-gray-200 px-3 py-2 dark:border-gray-700">
            <p className="text-lg font-semibold text-amber-600 dark:text-amber-400">{coverage.pending}</p>
            <p className="text-[11px] text-gray-400">receipt{coverage.pending === 1 ? '' : 's'} pending</p>
          </div>
          <div className="rounded-lg border border-gray-200 px-3 py-2 dark:border-gray-700">
            <p className="text-lg font-semibold text-blue-600 dark:text-blue-400">{Math.round(coverage.coveragePct)}%</p>
            <p className="text-[11px] text-gray-400">coverage</p>
          </div>
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
