import { useCallback, useEffect, useMemo, useState } from 'react';
import { AlertTriangle, Eye, Grid3X3 } from 'lucide-react';
import { ContextualHelpLink } from '@/components/help';
import { useDashboardApi } from '@/services/api';
import type {
  CodeTraceabilityEvidence,
  ObligationEvidenceMapping,
  CodeTraceabilityProjection,
} from '@/types';
import { ReceiptDetailModal } from './ReceiptDetailModal';
import {
  TraceabilityBadge,
  TraceabilityCurrentnessBadge,
  TraceabilityEmptyState,
} from './TraceabilityDisclosure';
import { projectedReceiptCurrentness } from './traceabilityCurrentness';
import { presentContextualEvidenceCoverage } from './sourceContextPresentation';

interface Props {
  boardId: string;
  subjectId: string;
  subjectVersion: number;
  boardSkipCoverage?: boolean;
  skipCoverage?: boolean;
  canEditCoverageFlags?: boolean;
  onSkipCoverageChange?: (skip: boolean) => Promise<void> | void;
  obligationTitles?: Readonly<Record<string, string>>;
}

const ENTITY_COLUMNS = [
  { label: 'Spec', kinds: ['spec'] },
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

const OBLIGATION_TYPE_LABELS: Record<string, string> = {
  spec: 'Spec',
  functional_requirement: 'Functional requirement',
  technical_requirement: 'Technical requirement',
  business_rule: 'Business rule',
  acceptance_criterion: 'Acceptance criterion',
  api_contract: 'API contract',
  integration_requirement: 'Integration requirement',
  observability_requirement: 'Observability requirement',
  decision: 'Decision',
  test_scenario: 'Test scenario',
};

function obligationTitle(
  mapping: ObligationEvidenceMapping,
  titles: Readonly<Record<string, string>>,
) {
  return titles[mapping.obligation_ref]
    || titles[mapping.obligation_id]
    || `${OBLIGATION_TYPE_LABELS[mapping.obligation_type] || 'Obligation'} ${shortId(mapping.obligation_id)}`;
}

function EntityLinks({
  mappings,
  obligationTitles,
}: {
  mappings: ObligationEvidenceMapping[];
  obligationTitles: Readonly<Record<string, string>>;
}) {
  if (mappings.length === 0) return <span className="text-gray-300 dark:text-gray-600">—</span>;
  return (
    <div className="flex min-w-[8rem] flex-col items-stretch gap-1">
      {mappings.map((mapping) => {
        const title = obligationTitle(mapping, obligationTitles);
        return (
          <span
            key={mapping.link_id}
            title={`${title} · ${mapping.relation_type.replace(/_/g, ' ')}`}
            className="line-clamp-2 max-w-[15rem] rounded bg-violet-50 px-2 py-1 text-left text-[10px] leading-4 text-violet-700 dark:bg-violet-950/40 dark:text-violet-300"
          >
            {title}
          </span>
        );
      })}
    </div>
  );
}

function MatrixRow({
  evidence,
  projection,
  obligationTitles,
  onViewReceipt,
}: {
  evidence: CodeTraceabilityEvidence;
  projection: CodeTraceabilityProjection;
  obligationTitles: Readonly<Record<string, string>>;
  onViewReceipt: (receiptId: string) => void;
}) {
  const mappings = (projection.obligation_evidence_mappings ?? []).filter(
    (mapping) => mapping.evidence_id === evidence.id,
  );
  const applicableMappings = mappings.filter(
    (mapping) => mapping.evidence_applicable === true,
  );
  const sourceContextItem = (projection.source_context_items ?? []).find(
    (item) => item.evidence_id === evidence.id,
  );
  const disposition = projection.dispositions.find(
    (candidate) => candidate.evidence_id === evidence.id && candidate.active,
  );
  const currentness = projectedReceiptCurrentness(
    projection,
    evidence.investigation_receipt_id,
  );
  const state = sourceContextItem?.evidence_applicable === false
    ? 'context_only'
    : sourceContextItem?.evidence_applicable === null
      ? 'needs_classification'
      : sourceContextItem?.evidence_applicable === true
        ? applicableMappings.length > 0
          ? 'linked'
          : disposition?.disposition || 'pending'
        : 'context_unavailable';
  const stateIsResolved = !['pending', 'needs_classification', 'context_unavailable'].includes(state);

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
          <EntityLinks
            mappings={applicableMappings.filter(
              (mapping) => (column.kinds as readonly string[]).includes(mapping.obligation_type),
            )}
            obligationTitles={obligationTitles}
          />
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
          !stateIsResolved
            ? 'bg-amber-50 text-amber-700 dark:bg-amber-950/30 dark:text-amber-300'
            : state === 'context_only'
              ? 'bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300'
              : 'bg-emerald-50 text-emerald-700 dark:bg-emerald-950/30 dark:text-emerald-300'
        }`}>
          {state === 'context_only'
            ? 'Context only'
            : state === 'needs_classification'
              ? 'Needs classification'
              : state === 'context_unavailable'
                ? 'Context unavailable'
                : state.replace(/_/g, ' ')}
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
  obligationTitles = {},
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
  const coverage = projection?.contextual_evidence_coverage;
  const coveragePresentation = useMemo(
    () => presentContextualEvidenceCoverage(coverage, projection?.source_context),
    [coverage, projection?.source_context],
  );
  const projectionIncomplete = coveragePresentation.kind === 'projection_incomplete';
  const coverageNotApplicable = coveragePresentation.kind === 'not_applicable';
  const projectionSkipCoverage = projection?.gate_readiness.evidence_coverage_skipped
    || false;

  const coverageStatus = useMemo(() => {
    const semanticStateMustRemainVisible = [
      'projection_unavailable',
      'projection_incomplete',
      'classification_required',
      'not_applicable',
      'investigation_partial',
      'investigation_unavailable',
      'not_calculated',
    ].includes(coveragePresentation.kind);
    if (!semanticStateMustRemainVisible && boardSkipCoverage) {
      return {
        label: 'Skipped by Board',
        className: 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
      };
    }
    if (!semanticStateMustRemainVisible && skipCoverage) {
      return {
        label: 'Skipped for this Spec',
        className: 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
      };
    }
    if (!semanticStateMustRemainVisible && projectionSkipCoverage) {
      return {
        label: 'Skipped',
        className: 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
      };
    }
    const classNameByKind: Record<typeof coveragePresentation.kind, string> = {
      projection_unavailable: 'bg-red-100 text-red-700 dark:bg-red-950/40 dark:text-red-300',
      projection_incomplete: 'bg-red-100 text-red-700 dark:bg-red-950/40 dark:text-red-300',
      classification_required: 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
      not_applicable: 'bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300',
      investigation_partial: 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
      investigation_unavailable: 'bg-red-100 text-red-700 dark:bg-red-950/40 dark:text-red-300',
      not_calculated: 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300',
      covered: 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300',
      pending: 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
    };
    return {
      label: coveragePresentation.label,
      className: classNameByKind[coveragePresentation.kind],
    };
  }, [boardSkipCoverage, coveragePresentation, projectionSkipCoverage, skipCoverage]);

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

      {projection && (
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
          {coveragePresentation.determinate && coveragePresentation.percentage !== null && (
            <div className="mb-3 h-2 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-700">
              <div
                role="progressbar"
                aria-label="Code Evidence coverage progress"
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={coveragePresentation.percentage}
                aria-valuetext={coveragePresentation.description}
                className={`h-full rounded-full transition-all duration-500 ${
                  coveragePresentation.kind === 'covered'
                    ? 'bg-green-500'
                    : 'bg-amber-500'
                }`}
                style={{ width: `${coveragePresentation.percentage}%` }}
              />
            </div>
          )}
          <p className="mb-3 text-[11px] text-gray-500 dark:text-gray-400">
            {coveragePresentation.description}
          </p>
          {coverage && !coverageNotApplicable && (
            <dl className="grid gap-2 sm:grid-cols-2 xl:grid-cols-5">
              <div className="rounded-lg border border-gray-200 px-3 py-2 dark:border-gray-700">
                <dt className="text-[11px] text-gray-400">applicable evidence total</dt>
                <dd
                  data-testid="contextual-evidence-total"
                  className="mt-0.5 text-lg font-semibold text-gray-900 dark:text-white"
                >
                  {coverage.total}
                </dd>
              </div>
              <div className="rounded-lg border border-gray-200 px-3 py-2 dark:border-gray-700">
                <dt className="text-[11px] text-gray-400">linked</dt>
                <dd
                  data-testid="contextual-evidence-linked"
                  className="mt-0.5 text-lg font-semibold text-emerald-600 dark:text-emerald-400"
                >
                  {coveragePresentation.countsAreLowerBounds ? '≥' : ''}{coverage.linked}
                </dd>
              </div>
              <div className="rounded-lg border border-gray-200 px-3 py-2 dark:border-gray-700">
                <dt className="text-[11px] text-gray-400">dispositioned</dt>
                <dd
                  data-testid="contextual-evidence-dispositioned"
                  className="mt-0.5 text-lg font-semibold text-violet-600 dark:text-violet-400"
                >
                  {coveragePresentation.countsAreLowerBounds ? '≥' : ''}{coverage.dispositioned}
                </dd>
              </div>
              <div className="rounded-lg border border-gray-200 px-3 py-2 dark:border-gray-700">
                <dt className="text-[11px] text-gray-400">pending</dt>
                <dd
                  data-testid="contextual-evidence-pending"
                  className="mt-0.5 text-lg font-semibold text-amber-600 dark:text-amber-400"
                >
                  {coveragePresentation.countsAreLowerBounds ? '≥' : ''}{coverage.pending}
                </dd>
              </div>
              <div className="rounded-lg border border-gray-200 px-3 py-2 dark:border-gray-700">
                <dt className="text-[11px] text-gray-400">coverage</dt>
                <dd
                  data-testid="contextual-evidence-coverage-pct"
                  className="mt-0.5 text-lg font-semibold text-blue-600 dark:text-blue-400"
                >
                  {coverage.coverage_pct === null
                    ? '—'
                    : `${coverage.coverage_pct}%`}
                </dd>
              </div>
            </dl>
          )}
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

      {boardSkipCoverage && !coverageNotApplicable && (
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

      {canEditCoverageFlags && onSkipCoverageChange && !coverageNotApplicable && (
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
      {!loading && !error && projection && evidence.length === 0 && !coverageNotApplicable && (
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
                <MatrixRow
                  key={item.id}
                  evidence={item}
                  projection={projection}
                  obligationTitles={obligationTitles}
                  onViewReceipt={setReceiptId}
                />
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
