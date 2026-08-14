import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle,
  BookOpenCheck,
  Clipboard,
  Eye,
  FileCode2,
  GitMerge,
  Link,
  Plus,
  ShieldAlert,
  Target,
} from 'lucide-react';
import toast from 'react-hot-toast';
import { useDashboardApi } from '@/services/api';
import type {
  CodeTraceabilityProjection,
  CodeTraceabilityWaiver,
  CodeTraceabilityWaiverCreateRequest,
  CodeTraceabilityWaiverReason,
  CodeTraceabilityWaiverScope,
  ImplementationOverlapProjection,
  ImplementationTargetExecutionRecordProjection,
  ImplementationTargetCreateRequest,
  ImplementationTargetProjection,
  ImplementationTargetResolution,
  ImplementationTargetRole,
  ImplementationTargetSelectorKind,
  TargetOverlapAcknowledgementRequest,
  TargetOverlapDisposition,
} from '@/types';
import { ReceiptDetailModal } from './ReceiptDetailModal';
import { SubmissionGuideDialog } from './SubmissionGuideDialog';
import {
  TraceabilityBadge,
  TraceabilityCurrentnessBadge,
  TraceabilityDisclosure,
} from './TraceabilityDisclosure';
import { projectedReceiptCurrentness } from './traceabilityCurrentness';
import { useCodeTraceabilityAuthority } from './useCodeTraceabilityAuthority';

interface Props {
  boardId: string;
  subjectId: string;
  subjectVersion: number;
  specVersion?: number | null;
  operationallyFrozen?: boolean;
  onCreateDependency?: () => void;
}

function shortId(value: string) {
  return value.length > 20 ? `${value.slice(0, 16)}…` : value;
}

const CARD_WAIVER_SCOPES: Array<{
  value: CodeTraceabilityWaiverScope;
  label: string;
}> = [
  { value: 'implementation_target', label: 'Implementation target' },
  { value: 'target_resolution', label: 'Target resolution' },
  { value: 'target_overlap', label: 'Target overlap' },
];

const WAIVER_REASONS: Array<{
  value: CodeTraceabilityWaiverReason;
  label: string;
}> = [
  { value: 'no_code_change', label: 'No code change' },
  { value: 'documentation_only', label: 'Documentation only' },
  { value: 'manual_process', label: 'Manual process' },
  { value: 'external_source_unavailable', label: 'External source unavailable' },
  { value: 'conceptual_board', label: 'Conceptual board' },
  { value: 'runtime_only', label: 'Runtime only' },
  { value: 'other', label: 'Other' },
];

function HumanWaiverSection({
  waivers,
  canCreate,
  canClear,
  onCreate,
  onClear,
}: {
  waivers: CodeTraceabilityWaiver[];
  canCreate: boolean;
  canClear: boolean;
  onCreate: (
    scope: CodeTraceabilityWaiverScope,
    reasonCode: CodeTraceabilityWaiverReason,
    justification: string,
  ) => Promise<void>;
  onClear: (waiverId: string) => Promise<void>;
}) {
  const [showForm, setShowForm] = useState(false);
  const [scope, setScope] = useState<CodeTraceabilityWaiverScope>('implementation_target');
  const [reasonCode, setReasonCode] = useState<CodeTraceabilityWaiverReason>('no_code_change');
  const [justification, setJustification] = useState('');
  const [saving, setSaving] = useState(false);
  const [clearingId, setClearingId] = useState<string | null>(null);
  const activeScopes = new Set(waivers.map((waiver) => waiver.scope));
  const availableScopes = CARD_WAIVER_SCOPES.filter(({ value }) => !activeScopes.has(value));

  const create = async () => {
    if (!justification.trim()) return;
    setSaving(true);
    try {
      await onCreate(scope, reasonCode, justification.trim());
      setShowForm(false);
      setJustification('');
    } catch {
      // The parent owns the user-facing error toast; keep the form open.
    } finally {
      setSaving(false);
    }
  };

  const clear = async (waiverId: string) => {
    setClearingId(waiverId);
    try {
      await onClear(waiverId);
    } catch {
      // The parent owns the user-facing error toast.
    } finally {
      setClearingId(null);
    }
  };

  return (
    <section
      className="rounded-lg border border-violet-200 bg-violet-50/30 p-3.5 dark:border-violet-900 dark:bg-violet-950/15"
      data-testid="card-human-waivers"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="flex items-center gap-1.5 text-xs font-semibold text-violet-900 dark:text-violet-200">
            <ShieldAlert size={14} /> Human waiver
          </h3>
          <p className="mt-1 text-[11px] leading-4 text-violet-700/80 dark:text-violet-300/80">
            Human exception · not attestation. A waiver covers one explicit gate scope; it never claims that source was inspected, resolved or executed.
          </p>
        </div>
        {canCreate && availableScopes.length > 0 && (
          <button
            type="button"
            onClick={() => {
              setScope(availableScopes[0].value);
              setShowForm((value) => !value);
            }}
            className="inline-flex items-center gap-1 rounded-md border border-violet-300 bg-white/70 px-2.5 py-1.5 text-[11px] font-medium text-violet-700 hover:bg-white dark:border-violet-800 dark:bg-gray-900/40 dark:text-violet-300"
          >
            <Plus size={12} /> Create human waiver
          </button>
        )}
      </div>

      {waivers.length === 0 ? (
        <p className="mt-3 text-[11px] text-gray-500 dark:text-gray-400">No active human waiver for this task.</p>
      ) : (
        <div className="mt-3 space-y-2">
          {waivers.map((waiver) => (
            <article key={waiver.id} className="rounded-md border border-violet-200 bg-white/80 px-3 py-2.5 dark:border-violet-900 dark:bg-gray-900/50">
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div>
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="rounded-full bg-violet-100 px-2 py-0.5 text-[10px] font-semibold text-violet-700 dark:bg-violet-950/50 dark:text-violet-300">
                      Human waiver
                    </span>
                    <span className="text-[10px] font-medium text-gray-500 dark:text-gray-400">
                      {waiver.scope.replace(/_/g, ' ')} · {waiver.reason_code.replace(/_/g, ' ')}
                    </span>
                  </div>
                  <p className="mt-1.5 text-[11px] leading-4 text-gray-600 dark:text-gray-300">{waiver.justification}</p>
                  <p className="mt-1 font-mono text-[10px] text-gray-400">Recorded by {waiver.created_by}</p>
                </div>
                {canClear && (
                  <button
                    type="button"
                    onClick={() => void clear(waiver.id)}
                    disabled={clearingId === waiver.id}
                    className="rounded-md border border-violet-300 px-2 py-1 text-[10px] font-medium text-violet-700 hover:bg-violet-50 disabled:opacity-50 dark:border-violet-800 dark:text-violet-300 dark:hover:bg-violet-950/30"
                  >
                    {clearingId === waiver.id ? 'Clearing…' : 'Clear waiver'}
                  </button>
                )}
              </div>
            </article>
          ))}
        </div>
      )}

      {showForm && canCreate && (
        <div className="mt-3 space-y-3 rounded-md border border-violet-200 bg-white/90 p-3 dark:border-violet-900 dark:bg-gray-900/70" role="region" aria-label="Create human waiver">
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="text-[11px] font-medium text-gray-600 dark:text-gray-300">
              Gate scope
              <select
                aria-label="Waiver gate scope"
                value={scope}
                onChange={(event) => setScope(event.target.value as CodeTraceabilityWaiverScope)}
                className="mt-1 w-full rounded-md border border-violet-200 bg-white px-2.5 py-2 text-xs dark:border-violet-900 dark:bg-gray-800"
              >
                {availableScopes.map(({ value, label }) => <option key={value} value={value}>{label}</option>)}
              </select>
            </label>
            <label className="text-[11px] font-medium text-gray-600 dark:text-gray-300">
              Reason
              <select
                aria-label="Waiver reason"
                value={reasonCode}
                onChange={(event) => setReasonCode(event.target.value as CodeTraceabilityWaiverReason)}
                className="mt-1 w-full rounded-md border border-violet-200 bg-white px-2.5 py-2 text-xs dark:border-violet-900 dark:bg-gray-800"
              >
                {WAIVER_REASONS.map(({ value, label }) => <option key={value} value={value}>{label}</option>)}
              </select>
            </label>
          </div>
          <textarea
            aria-label="Waiver justification"
            value={justification}
            onChange={(event) => setJustification(event.target.value)}
            placeholder="Explain why this exact gate scope is not applicable…"
            rows={3}
            className="w-full resize-none rounded-md border border-violet-200 bg-white px-3 py-2 text-xs dark:border-violet-900 dark:bg-gray-800"
          />
          <div className="flex justify-end gap-2">
            <button type="button" onClick={() => setShowForm(false)} className="btn btn-secondary text-xs">Cancel</button>
            <button
              type="button"
              onClick={() => void create()}
              disabled={saving || !justification.trim()}
              className="rounded-md bg-violet-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-violet-700 disabled:opacity-50"
            >
              {saving ? 'Recording…' : 'Record human waiver'}
            </button>
          </div>
        </div>
      )}
    </section>
  );
}

function locationLabel(
  target: ImplementationTargetProjection,
  resolution: ImplementationTargetResolution | undefined,
) {
  const path = resolution?.resolved_relative_path || target.relative_path_hint;
  if (!path) return 'No location included in the accepted resolution';
  const start = resolution?.resolved_line_start;
  const end = resolution?.resolved_line_end;
  if (!start) return path;
  return `${path}:L${start}${end && end !== start ? `–${end}` : ''}`;
}

function executionLocationLabel(
  execution: ImplementationTargetExecutionRecordProjection,
) {
  if (execution.actual_qualified_symbol && execution.actual_relative_path) {
    return `${execution.actual_qualified_symbol} · ${execution.actual_relative_path}`;
  }
  return execution.actual_qualified_symbol || execution.actual_relative_path || null;
}

function OverlapNotice({
  overlap,
  targetId,
  canAcknowledge,
  onAcknowledge,
  onCreateDependency,
}: {
  overlap: ImplementationOverlapProjection;
  targetId: string;
  canAcknowledge: boolean;
  onAcknowledge: (payload: TargetOverlapAcknowledgementRequest) => Promise<void>;
  onCreateDependency?: () => void;
}) {
  const [showForm, setShowForm] = useState(false);
  const [disposition, setDisposition] = useState<TargetOverlapDisposition>('ordered_by_dependency');
  const [justification, setJustification] = useState('');
  const [saving, setSaving] = useState(false);
  const peerId = overlap.target_a_id === targetId
    ? overlap.target_b_id
    : overlap.target_a_id;

  const submit = async () => {
    if (!justification.trim()) return;
    setSaving(true);
    try {
      await onAcknowledge({
        target_a_id: overlap.target_a_id,
        target_b_id: overlap.target_b_id,
        resolution_a_id: overlap.resolution_a_id,
        resolution_b_id: overlap.resolution_b_id,
        disposition,
        justification: justification.trim(),
      });
      setShowForm(false);
      setJustification('');
    } catch {
      // The parent owns the user-facing error toast; keep the form open.
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="rounded-md bg-amber-50 px-2.5 py-2 text-[11px] text-amber-800 dark:bg-amber-950/30 dark:text-amber-300">
      <p>
        <span className="font-semibold capitalize">{overlap.severity}</span>
        {' · '}Target {shortId(peerId)} also affects {overlap.qualified_symbol || overlap.relative_path || 'the same submitted location'}.
        {overlap.acknowledgement && (
          <span className="ml-1 font-medium">Acknowledged: {overlap.acknowledgement.disposition.replace(/_/g, ' ')}</span>
        )}
      </p>

      <div className="mt-2 flex flex-wrap gap-2">
        {onCreateDependency && (
          <button
            type="button"
            onClick={onCreateDependency}
            className="inline-flex items-center gap-1 rounded border border-amber-300 bg-white/70 px-2 py-1 font-medium hover:bg-white dark:border-amber-800 dark:bg-gray-900/40"
          >
            <Link size={11} /> Create dependency
          </button>
        )}
        {canAcknowledge && !overlap.acknowledgement && (
          <button
            type="button"
            onClick={() => setShowForm((value) => !value)}
            className="rounded border border-amber-300 bg-white/70 px-2 py-1 font-medium hover:bg-white dark:border-amber-800 dark:bg-gray-900/40"
          >
            Acknowledge overlap
          </button>
        )}
      </div>

      {showForm && canAcknowledge && (
        <div className="mt-2 space-y-2 rounded border border-amber-200 bg-white/80 p-2 dark:border-amber-900 dark:bg-gray-900/70">
          <label className="block">
            <span className="mb-1 block font-medium">Disposition</span>
            <select
              aria-label="Overlap disposition"
              value={disposition}
              onChange={(event) => setDisposition(event.target.value as TargetOverlapDisposition)}
              className="w-full rounded border border-amber-200 bg-white px-2 py-1.5 text-xs text-gray-800 dark:border-amber-900 dark:bg-gray-800 dark:text-gray-100"
            >
              <option value="ordered_by_dependency">Ordered by dependency</option>
              <option value="accepted_parallel">Accepted parallel</option>
              <option value="merged_targets">Merged targets</option>
              <option value="false_positive">False positive</option>
            </select>
          </label>
          <textarea
            aria-label="Overlap acknowledgement justification"
            value={justification}
            onChange={(event) => setJustification(event.target.value)}
            placeholder="Explain how the overlap will be handled…"
            rows={2}
            className="w-full resize-none rounded border border-amber-200 bg-white px-2 py-1.5 text-xs text-gray-800 dark:border-amber-900 dark:bg-gray-800 dark:text-gray-100"
          />
          <div className="flex justify-end gap-2">
            <button type="button" onClick={() => setShowForm(false)} className="rounded px-2 py-1 text-gray-500">
              Cancel
            </button>
            <button
              type="button"
              onClick={() => void submit()}
              disabled={saving || !justification.trim()}
              className="rounded bg-amber-600 px-2 py-1 font-medium text-white disabled:opacity-50"
            >
              {saving ? 'Appending…' : 'Append acknowledgement'}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function TargetCard({
  target,
  projection,
  onViewReceipt,
  canAcknowledgeOverlap,
  onAcknowledgeOverlap,
  onCreateDependency,
}: {
  target: ImplementationTargetProjection;
  projection: CodeTraceabilityProjection;
  onViewReceipt: (receiptId: string) => void;
  canAcknowledgeOverlap: boolean;
  onAcknowledgeOverlap: (payload: TargetOverlapAcknowledgementRequest) => Promise<void>;
  onCreateDependency?: () => void;
}) {
  const resolution = projection.resolutions.find(
    (candidate) => candidate.id === target.current_resolution_id,
  ) || projection.resolutions.find((candidate) => candidate.target_id === target.id);
  const freshness = projection.resolution_freshness?.[target.id]
    || (resolution ? projection.resolution_freshness?.[resolution.id] : undefined)
    || projection.gate_readiness.resolution_freshness?.[target.id]
    || (resolution ? projection.gate_readiness.resolution_freshness?.[resolution.id] : undefined);
  const executions = projection.executions?.filter(
    (execution) => execution.target_id === target.id,
  );
  const overlaps = projection.overlaps.filter(
    (overlap: ImplementationOverlapProjection) => overlap.target_a_id === target.id || overlap.target_b_id === target.id,
  );
  const currentness = freshness?.currentness ?? 'unknown';
  const current = currentness === 'current';
  const resolvedSymbol = resolution?.resolved_qualified_symbol || target.qualified_symbol;

  return (
    <article className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm dark:border-gray-700 dark:bg-gray-800/70">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className={`inline-flex rounded-full px-2 py-0.5 text-[10px] font-bold uppercase ${
              target.role === 'modify'
                ? 'bg-amber-50 text-amber-700 dark:bg-amber-950/30 dark:text-amber-300'
                : target.role === 'create'
                  ? 'bg-emerald-50 text-emerald-700 dark:bg-emerald-950/30 dark:text-emerald-300'
                  : 'bg-blue-50 text-blue-700 dark:bg-blue-950/30 dark:text-blue-300'
            }`}>
              {target.role}
            </span>
            {target.required && (
              <span className="text-[10px] font-medium text-gray-400">required</span>
            )}
            <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[10px] capitalize text-gray-500 dark:bg-gray-700 dark:text-gray-400">
              {target.lifecycle_status}
            </span>
          </div>
          <h3 className="mt-2 truncate font-mono text-sm font-semibold text-gray-900 dark:text-white" title={resolvedSymbol || target.intent}>
            {resolvedSymbol || target.intent || 'Semantic implementation target'}
          </h3>
          {target.intent && resolvedSymbol && (
            <p className="mt-1 text-xs leading-5 text-gray-500 dark:text-gray-400">{target.intent}</p>
          )}
        </div>
        {resolution && (
          <button
            type="button"
            onClick={() => onViewReceipt(resolution.investigation_receipt_id)}
            className="inline-flex shrink-0 items-center gap-1 rounded-md border border-gray-200 px-2.5 py-1.5 text-[11px] font-medium text-gray-600 hover:bg-gray-50 dark:border-gray-700 dark:text-gray-300 dark:hover:bg-gray-700"
          >
            <Eye size={12} /> View resolution receipt
          </button>
        )}
      </div>

      {resolution ? (
        <div className="mt-3 space-y-2 rounded-md border border-cyan-100 bg-cyan-50/50 px-3 py-2.5 dark:border-cyan-900/70 dark:bg-cyan-950/20">
          <div className="flex flex-wrap items-center gap-1.5">
            <TraceabilityBadge kind="agent-attested" />
            <span className="text-[11px] font-medium text-cyan-800 dark:text-cyan-200">
              Agent-attested resolution · PF-{resolution.receipt_generation}
            </span>
          </div>
          <div className="flex min-w-0 items-center gap-2 text-xs text-gray-700 dark:text-gray-200">
            <FileCode2 size={13} className="shrink-0 text-gray-400" />
            <span className="truncate font-mono" title={locationLabel(target, resolution)}>
              {locationLabel(target, resolution)}
            </span>
          </div>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-gray-500 dark:text-gray-400">
            <TraceabilityCurrentnessBadge currentness={currentness} />
            <span>{current
              ? `Current against preflight PF-${resolution.receipt_generation}`
              : `${currentness.replace(/_/g, ' ')} · ${freshness?.state?.replace(/_/g, ' ') || 'freshness unavailable'}`}</span>
            {resolution.confidence != null && <span>{Math.round(resolution.confidence * 100)}% confidence declared</span>}
          </div>
        </div>
      ) : (
        <div className="mt-3 rounded-md border border-dashed border-gray-300 px-3 py-3 text-xs text-gray-400 dark:border-gray-700">
          No agent-attested resolution has been accepted for this target.
        </div>
      )}

      {executions && (
        <section
          className="mt-3 space-y-2 border-t border-gray-100 pt-3 dark:border-gray-700"
          data-testid={`target-executions-${target.id}`}
        >
          <h4 className="text-[11px] font-semibold text-gray-600 dark:text-gray-300">
            Agent-submitted execution receipts
          </h4>
          {executions.length === 0 ? (
            <p className="rounded-md border border-dashed border-gray-300 px-3 py-2.5 text-[11px] text-gray-400 dark:border-gray-700">
              No execution receipt has been accepted for this target.
            </p>
          ) : executions.map((execution) => {
            const currentTargetRevision = execution.target_revision === target.revision;
            const executionCurrentness = projectedReceiptCurrentness(
              projection,
              execution.result_investigation_receipt_id,
            );
            const executionLocation = executionLocationLabel(execution);

            return (
              <article
                key={execution.id}
                className="rounded-md border border-emerald-100 bg-emerald-50/40 px-3 py-2.5 dark:border-emerald-900/70 dark:bg-emerald-950/15"
              >
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <TraceabilityBadge kind="receipt-accepted" />
                    <span className="text-[11px] font-medium text-emerald-800 dark:text-emerald-200">
                      Agent-submitted execution · {execution.disposition.replace(/_/g, ' ')}
                    </span>
                  </div>
                  <button
                    type="button"
                    onClick={() => onViewReceipt(execution.result_investigation_receipt_id)}
                    className="inline-flex shrink-0 items-center gap-1 rounded-md border border-emerald-200 bg-white/70 px-2 py-1 text-[10px] font-medium text-emerald-700 hover:bg-white dark:border-emerald-900 dark:bg-gray-900/40 dark:text-emerald-300"
                  >
                    <Eye size={11} /> View execution receipt
                  </button>
                </div>
                {executionLocation && (
                  <div className="mt-2 flex min-w-0 items-center gap-2 text-[11px] text-gray-700 dark:text-gray-200">
                    <FileCode2 size={12} className="shrink-0 text-gray-400" />
                    <span className="truncate font-mono" title={executionLocation}>
                      {executionLocation}
                    </span>
                  </div>
                )}
                <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-gray-500 dark:text-gray-400">
                  <TraceabilityCurrentnessBadge currentness={executionCurrentness} />
                  <span>{currentTargetRevision
                    ? `Target r${execution.target_revision}`
                    : `Historical target r${execution.target_revision} · current r${target.revision}`}</span>
                  {execution.result_declared_revision && (
                    <span className="font-mono">Result {shortId(execution.result_declared_revision)}</span>
                  )}
                </div>
                {execution.justification && (
                  <p className="mt-2 text-[11px] leading-4 text-gray-600 dark:text-gray-300">
                    {execution.justification}
                  </p>
                )}
              </article>
            );
          })}
        </section>
      )}

      <dl className="mt-3 grid gap-x-4 gap-y-2 text-[11px] sm:grid-cols-3">
        <div className="min-w-0">
          <dt className="text-gray-400">Logical source</dt>
          <dd className="truncate font-mono text-gray-600 dark:text-gray-300" title={target.source_ref}>{target.source_ref}</dd>
        </div>
        <div className="min-w-0">
          <dt className="text-gray-400">Selector</dt>
          <dd className="truncate text-gray-600 dark:text-gray-300">{target.selector_kind.replace(/_/g, ' ')}</dd>
        </div>
        <div className="min-w-0">
          <dt className="text-gray-400">Target ID</dt>
          <dd className="truncate font-mono text-gray-600 dark:text-gray-300" title={target.id}>{shortId(target.id)}</dd>
        </div>
      </dl>

      {overlaps.length > 0 && (
        <div className="mt-3 space-y-2 border-t border-gray-100 pt-3 dark:border-gray-700">
          <div className="flex items-center gap-1.5 text-[11px] font-semibold text-amber-700 dark:text-amber-300">
            <GitMerge size={13} /> Overlap
          </div>
          {overlaps.map((overlap) => {
            return (
              <OverlapNotice
                key={`${overlap.resolution_a_id}:${overlap.resolution_b_id}`}
                overlap={overlap}
                targetId={target.id}
                canAcknowledge={canAcknowledgeOverlap}
                onAcknowledge={onAcknowledgeOverlap}
                onCreateDependency={onCreateDependency}
              />
            );
          })}
        </div>
      )}
    </article>
  );
}

function AddTargetForm({
  projection,
  specVersion,
  onCancel,
  onSubmit,
}: {
  projection: CodeTraceabilityProjection;
  specVersion: number | null | undefined;
  onCancel: () => void;
  onSubmit: (payload: ImplementationTargetCreateRequest) => Promise<void>;
}) {
  const sourceRefs = projection.heads
    .filter((head) => Boolean(head.current_receipt_id))
    .map((head) => head.source_ref);
  const [sourceRef, setSourceRef] = useState(sourceRefs[0] ?? '');
  const [selectorKind, setSelectorKind] = useState<ImplementationTargetSelectorKind>('semantic');
  const [role, setRole] = useState<ImplementationTargetRole>('modify');
  const [intent, setIntent] = useState('');
  const [relativePath, setRelativePath] = useState('');
  const [qualifiedSymbol, setQualifiedSymbol] = useState('');
  const [required, setRequired] = useState(true);
  const [saving, setSaving] = useState(false);

  const needsPath = selectorKind === 'file' || selectorKind === 'new_file';
  const needsSymbol = selectorKind === 'symbol';
  const valid = Boolean(
    sourceRef
    && intent.trim()
    && specVersion
    && (!needsPath || relativePath.trim())
    && (!needsSymbol || qualifiedSymbol.trim()),
  );

  const submit = async () => {
    if (!valid || !specVersion) return;
    setSaving(true);
    try {
      await onSubmit({
        source_ref: sourceRef,
        selector_kind: selectorKind,
        relative_path_hint: relativePath.trim() || null,
        language: null,
        symbol_kind: null,
        qualified_symbol: qualifiedSymbol.trim() || null,
        symbol_signature: null,
        role,
        intent: intent.trim(),
        required,
        expected_spec_version: specVersion,
        baseline_evidence_id: null,
        spec_links: [],
        evidence_links: [],
      });
    } catch {
      // The parent owns the user-facing error toast; keep the form open.
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="space-y-3 rounded-lg border border-blue-200 bg-blue-50/40 p-4 dark:border-blue-900 dark:bg-blue-950/20" aria-label="Add semantic target">
      <div>
        <h3 className="text-sm font-semibold text-gray-900 dark:text-white">Add semantic target</h3>
        <p className="mt-0.5 text-[11px] leading-4 text-gray-500 dark:text-gray-400">
          Record the implementation intent. An authenticated external agent must submit any concrete resolution.
        </p>
      </div>

      {sourceRefs.length === 0 && (
        <div className="rounded border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] text-amber-700 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-300">
          A current agent-submitted preflight receipt is required before this target can be recorded.
        </div>
      )}

      <div className="grid gap-3 sm:grid-cols-2">
        <label className="block text-[11px] font-medium text-gray-600 dark:text-gray-300">
          Logical source
          <select
            aria-label="Target logical source"
            value={sourceRef}
            onChange={(event) => setSourceRef(event.target.value)}
            className="mt-1 w-full rounded-md border border-gray-300 bg-white px-2.5 py-2 font-mono text-xs dark:border-gray-700 dark:bg-gray-800"
          >
            {sourceRefs.length === 0 && <option value="">No current source receipt</option>}
            {sourceRefs.map((value) => <option key={value} value={value}>{value}</option>)}
          </select>
        </label>
        <label className="block text-[11px] font-medium text-gray-600 dark:text-gray-300">
          Role
          <select
            aria-label="Target role"
            value={role}
            onChange={(event) => setRole(event.target.value as ImplementationTargetRole)}
            className="mt-1 w-full rounded-md border border-gray-300 bg-white px-2.5 py-2 text-xs dark:border-gray-700 dark:bg-gray-800"
          >
            {(['modify', 'read', 'extend', 'create', 'delete', 'test', 'validate'] as const).map((value) => (
              <option key={value} value={value}>{value}</option>
            ))}
          </select>
        </label>
        <label className="block text-[11px] font-medium text-gray-600 dark:text-gray-300">
          Selector
          <select
            aria-label="Target selector kind"
            value={selectorKind}
            onChange={(event) => setSelectorKind(event.target.value as ImplementationTargetSelectorKind)}
            className="mt-1 w-full rounded-md border border-gray-300 bg-white px-2.5 py-2 text-xs dark:border-gray-700 dark:bg-gray-800"
          >
            <option value="semantic">Semantic</option>
            <option value="symbol">Symbol</option>
            <option value="file">File</option>
            <option value="glob">Glob</option>
            <option value="new_file">New file</option>
          </select>
        </label>
        <label className="block text-[11px] font-medium text-gray-600 dark:text-gray-300">
          Path hint {needsPath ? '(required)' : '(optional)'}
          <input
            aria-label="Target relative path hint"
            value={relativePath}
            onChange={(event) => setRelativePath(event.target.value)}
            placeholder="src/domain/service.ts"
            className="mt-1 w-full rounded-md border border-gray-300 bg-white px-2.5 py-2 font-mono text-xs dark:border-gray-700 dark:bg-gray-800"
          />
        </label>
        <label className="block text-[11px] font-medium text-gray-600 dark:text-gray-300 sm:col-span-2">
          Qualified symbol {needsSymbol ? '(required)' : '(optional)'}
          <input
            aria-label="Target qualified symbol"
            value={qualifiedSymbol}
            onChange={(event) => setQualifiedSymbol(event.target.value)}
            placeholder="PaymentsService.authorize"
            className="mt-1 w-full rounded-md border border-gray-300 bg-white px-2.5 py-2 font-mono text-xs dark:border-gray-700 dark:bg-gray-800"
          />
        </label>
        <label className="block text-[11px] font-medium text-gray-600 dark:text-gray-300 sm:col-span-2">
          Intent
          <textarea
            aria-label="Target intent"
            value={intent}
            onChange={(event) => setIntent(event.target.value)}
            placeholder="Describe what this task intends to change or validate…"
            rows={3}
            className="mt-1 w-full resize-none rounded-md border border-gray-300 bg-white px-2.5 py-2 text-xs dark:border-gray-700 dark:bg-gray-800"
          />
        </label>
      </div>

      <label className="flex items-center gap-2 text-[11px] text-gray-600 dark:text-gray-300">
        <input type="checkbox" checked={required} onChange={(event) => setRequired(event.target.checked)} />
        Required for task completion
      </label>

      <div className="flex justify-end gap-2">
        <button type="button" onClick={onCancel} className="btn btn-secondary text-xs">Cancel</button>
        <button
          type="button"
          onClick={() => void submit()}
          disabled={!valid || saving}
          className="btn btn-primary text-xs disabled:opacity-50"
        >
          {saving ? 'Adding…' : 'Add semantic target'}
        </button>
      </div>
    </section>
  );
}

export function ImplementationTargetsPanel({
  boardId,
  subjectId,
  subjectVersion,
  specVersion,
  operationallyFrozen = false,
  onCreateDependency,
}: Props) {
  const api = useDashboardApi();
  const {
    canCreateTarget,
    canAcknowledgeOverlap,
    canCreateWaiver,
    canClearWaiver,
  } = useCodeTraceabilityAuthority(boardId);
  const [projection, setProjection] = useState<CodeTraceabilityProjection | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [receiptId, setReceiptId] = useState<string | null>(null);
  const [showAddTarget, setShowAddTarget] = useState(false);
  const [showSubmissionGuide, setShowSubmissionGuide] = useState(false);

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError(null);
    try {
      setProjection(await api.getCodeTraceabilityProjection(
        boardId,
        'card',
        subjectId,
        subjectVersion,
        'detail',
        signal,
      ));
    } catch (caught) {
      if (!signal?.aborted) {
        setError(caught instanceof Error ? caught.message : 'Could not load implementation targets.');
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

  const targets = useMemo(() => projection?.targets ?? [], [projection]);

  const createTarget = async (payload: ImplementationTargetCreateRequest) => {
    try {
      await api.createImplementationTarget(boardId, subjectId, payload);
      toast.success('Semantic target added');
      setShowAddTarget(false);
      await load();
    } catch (caught) {
      toast.error(caught instanceof Error ? caught.message : 'Could not add semantic target.');
      throw caught;
    }
  };

  const acknowledgeOverlap = async (payload: TargetOverlapAcknowledgementRequest) => {
    try {
      await api.acknowledgeImplementationOverlap(boardId, subjectId, payload);
      toast.success('Overlap acknowledgement appended');
      await load();
    } catch (caught) {
      toast.error(caught instanceof Error ? caught.message : 'Could not acknowledge overlap.');
      throw caught;
    }
  };

  const createWaiver = async (
    scope: CodeTraceabilityWaiverScope,
    reasonCode: CodeTraceabilityWaiverReason,
    justification: string,
  ) => {
    const payload: CodeTraceabilityWaiverCreateRequest = {
      entity_type: 'card',
      entity_id: subjectId,
      scope,
      reason_code: reasonCode,
      justification,
    };
    try {
      await api.createCodeTraceabilityWaiver(boardId, payload);
      toast.success('Human waiver recorded');
      await load();
    } catch (caught) {
      toast.error(caught instanceof Error ? caught.message : 'Could not record human waiver.');
      throw caught;
    }
  };

  const clearWaiver = async (waiverId: string) => {
    try {
      await api.clearCodeTraceabilityWaiver(boardId, waiverId);
      toast.success('Human waiver cleared');
      await load();
    } catch (caught) {
      toast.error(caught instanceof Error ? caught.message : 'Could not clear human waiver.');
      throw caught;
    }
  };

  return (
    <div className="space-y-4" data-testid="card-implementation-targets-panel">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="flex items-center gap-1.5 text-sm font-semibold text-gray-900 dark:text-white">
            <Target size={15} className="text-gray-400" /> Implementation targets
          </h2>
          <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
            Semantic target intentions may be added by people; concrete resolutions remain agent-attested.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            onClick={() => setShowSubmissionGuide(true)}
            className="inline-flex items-center gap-1.5 rounded-md border border-gray-200 px-2.5 py-1.5 text-[11px] font-medium text-gray-600 hover:bg-gray-50 dark:border-gray-700 dark:text-gray-300 dark:hover:bg-gray-700"
          >
            <BookOpenCheck size={12} /> Submission guide
          </button>
          <button
            type="button"
            onClick={() => {
              void navigator.clipboard.writeText(`card:${subjectId}@${subjectVersion}`);
              toast.success('Task context copied');
            }}
            className="inline-flex items-center gap-1.5 rounded-md border border-gray-200 px-2.5 py-1.5 text-[11px] font-medium text-gray-600 hover:bg-gray-50 dark:border-gray-700 dark:text-gray-300 dark:hover:bg-gray-700"
          >
            <Clipboard size={12} /> Copy task context
          </button>
          {canCreateTarget && !operationallyFrozen && (
            <button
              type="button"
              onClick={() => setShowAddTarget((value) => !value)}
              disabled={!projection || !specVersion}
              title={!specVersion ? 'The current Spec version is required' : undefined}
              className="inline-flex items-center gap-1.5 rounded-md bg-blue-600 px-2.5 py-1.5 text-[11px] font-medium text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <Plus size={12} /> Add semantic target
            </button>
          )}
        </div>
      </div>

      <TraceabilityDisclosure />
      {operationallyFrozen && (
        <div
          className="flex items-start gap-2 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-[11px] leading-5 text-rose-800 dark:border-rose-900 dark:bg-rose-950/30 dark:text-rose-300"
          role="note"
        >
          <AlertTriangle size={14} className="mt-0.5 shrink-0" />
          <span>
            This card is Rejected. Targets and governance records are read-only until it moves to In Progress; an authenticated agent may still renew the Current target resolution used to authorize that transition.
          </span>
        </div>
      )}
      <div className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] leading-5 text-amber-800 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-300" role="note">
        <AlertTriangle size={14} className="mt-0.5 shrink-0" />
        <span>Pulse cannot detect source changes until an agent submits a newer preflight receipt.</span>
      </div>

      {projection && (
        <HumanWaiverSection
          waivers={projection.waivers ?? []}
          canCreate={canCreateWaiver && !operationallyFrozen}
          canClear={canClearWaiver && !operationallyFrozen}
          onCreate={createWaiver}
          onClear={clearWaiver}
        />
      )}

      {showAddTarget && canCreateTarget && !operationallyFrozen && projection && (
        <AddTargetForm
          projection={projection}
          specVersion={specVersion}
          onCancel={() => setShowAddTarget(false)}
          onSubmit={createTarget}
        />
      )}

      {loading && <div className="py-10 text-center text-xs text-gray-400" role="status">Loading accepted target resolutions…</div>}
      {error && (
        <div className="flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300" role="alert">
          <AlertTriangle size={14} className="mt-0.5 shrink-0" />
          <span>{error}</span>
        </div>
      )}
      {!loading && !error && projection && targets.length === 0 && (
        <div className="rounded-lg border border-dashed border-gray-300 px-5 py-8 text-center dark:border-gray-700">
          <p className="text-sm font-medium text-gray-600 dark:text-gray-300">
            No implementation targets yet
          </p>
          <p className="mx-auto mt-1 max-w-lg text-xs leading-5 text-gray-400 dark:text-gray-500">
            A person with target authority can add semantic intent here. Pulse waits for an authenticated external agent to submit any concrete source resolution.
          </p>
        </div>
      )}
      {!loading && !error && projection && targets.length > 0 && (
        <div className="space-y-3">
          {targets.map((target) => (
            <TargetCard
              key={target.id}
              target={target}
              projection={projection}
              onViewReceipt={setReceiptId}
              canAcknowledgeOverlap={canAcknowledgeOverlap && !operationallyFrozen}
              onAcknowledgeOverlap={acknowledgeOverlap}
              onCreateDependency={onCreateDependency}
            />
          ))}
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
      {showSubmissionGuide && (
        <SubmissionGuideDialog
          boardId={boardId}
          subjectType="card"
          subjectId={subjectId}
          subjectVersion={subjectVersion}
          onClose={() => setShowSubmissionGuide(false)}
        />
      )}
    </div>
  );
}
