import {
  ChevronLeft,
  ChevronRight,
  Filter,
  GitBranch,
  Loader2,
  Lock,
  Plus,
  RefreshCw,
  Send,
  X,
} from 'lucide-react';
import {
  type FormEvent,
  useEffect,
  useRef,
  useState,
} from 'react';
import toast from 'react-hot-toast';

import { usePermissions } from '@/hooks/usePermissions';
import {
  ResearchDecisionApiError,
  useResearchDecisionsApi,
} from '@/services/research-decisions-api';
import {
  RESEARCH_DECISION_PAGE_SIZES,
  type LegacyRefinementDecision,
  type ResearchDecisionAnchorType,
  type ResearchDecisionContent,
  type ResearchDecisionEntry,
  type ResearchDecisionPage,
  type ResearchDecisionPageSize,
  type ResearchDecisionStatus,
} from '@/types/research-decisions';

const READ_PERMISSION = 'refinement.research_decisions.read';
const APPEND_PERMISSION = 'refinement.research_decisions.append';

const STATUS_LABELS: Record<ResearchDecisionStatus, string> = {
  open: 'Open',
  investigating: 'Investigating',
  resolved: 'Resolved',
  deferred: 'Deferred',
};

const STATUS_STYLES: Record<ResearchDecisionStatus, string> = {
  open: 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-200',
  investigating:
    'bg-violet-100 text-violet-700 dark:bg-violet-900/30 dark:text-violet-200',
  resolved:
    'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-200',
  deferred:
    'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-200',
};

const ANCHOR_LABELS: Record<ResearchDecisionAnchorType, string> = {
  functional_requirement: 'Functional requirement',
  acceptance_criterion: 'Acceptance criterion',
  technical_requirement: 'Technical requirement',
  qa: 'Q&A',
};

interface ResearchDecisionDraft {
  unknown: string;
  status: ResearchDecisionStatus;
  anchorType: ResearchDecisionAnchorType;
  anchorRef: string;
  evidenceRefs: string;
  alternatives: string;
  decision: string;
  rationale: string;
  confidence: string;
  evidenceAbsenceJustification: string;
}

interface ResearchDecisionEditorState {
  operation: 'append' | 'supersede';
  draft: ResearchDecisionDraft;
  ledgerId: string | null;
  predecessorEntryId: string | null;
  expectedHeadRevision: number;
}

export interface ResearchDecisionPanelProps {
  boardId: string;
  refinementId: string;
  refinementStatus: string;
  refinementArchived?: boolean;
  /**
   * Historical `Refinement.decisions: string[]`. These values are rendered in
   * a separate read-only block and never enter an RDL request.
   */
  legacyDecisions?: readonly LegacyRefinementDecision[] | null;
  className?: string;
  /** Allows the host modal to refresh its displayed Refinement version. */
  onRefinementVersionChanged?: (version: number) => void;
}

function newIdempotencyKey(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `rdl-ui-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

const RDL_SESSION_INTENT_CACHE_LIMIT = 32;

function idempotencyKeyForFingerprint(
  cache: Map<string, string>,
  fingerprint: string,
): string {
  const existing = cache.get(fingerprint);
  if (existing) {
    cache.delete(fingerprint);
    cache.set(fingerprint, existing);
    return existing;
  }
  const created = newIdempotencyKey();
  cache.set(fingerprint, created);
  while (cache.size > RDL_SESSION_INTENT_CACHE_LIMIT) {
    const oldest = cache.keys().next().value;
    if (oldest === undefined) break;
    cache.delete(oldest);
  }
  return created;
}

function emptyDraft(): ResearchDecisionDraft {
  return {
    unknown: '',
    status: 'open',
    anchorType: 'functional_requirement',
    anchorRef: '',
    evidenceRefs: '',
    alternatives: '',
    decision: '',
    rationale: '',
    confidence: '',
    evidenceAbsenceJustification: '',
  };
}

function draftFromEntry(entry: ResearchDecisionEntry): ResearchDecisionDraft {
  return {
    unknown: entry.content.unknown,
    status: entry.content.status,
    anchorType: entry.content.anchor.anchor_type,
    anchorRef: entry.content.anchor.anchor_ref,
    evidenceRefs: entry.content.evidence_refs.join('\n'),
    alternatives: entry.content.alternatives.join('\n'),
    decision: entry.content.decision ?? '',
    rationale: entry.content.rationale ?? '',
    confidence:
      entry.content.confidence === null
        ? ''
        : String(entry.content.confidence),
    evidenceAbsenceJustification:
      entry.content.evidence_absence_justification ?? '',
  };
}

function normalizedLines(value: string): string[] {
  return Array.from(
    new Set(
      value
        .split(/\r?\n/)
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  );
}

function optionalText(value: string): string | null {
  const normalized = value.trim();
  return normalized || null;
}

function contentFromDraft(
  draft: ResearchDecisionDraft,
): { content: ResearchDecisionContent | null; error: string | null } {
  const unknown = draft.unknown.trim();
  const anchorRef = draft.anchorRef.trim();
  if (!unknown) {
    return { content: null, error: 'Describe the unknown being investigated.' };
  }
  if (!anchorRef) {
    return { content: null, error: 'A stable FR, AC, TR, or Q&A anchor is required.' };
  }

  let confidence: number | null = null;
  if (draft.confidence.trim()) {
    confidence = Number(draft.confidence);
    if (!Number.isFinite(confidence) || confidence < 0 || confidence > 1) {
      return { content: null, error: 'Confidence must be between 0 and 1.' };
    }
  }

  const evidenceRefs = normalizedLines(draft.evidenceRefs);
  const evidenceAbsenceJustification = optionalText(
    draft.evidenceAbsenceJustification,
  );
  const decision = optionalText(draft.decision);
  const rationale = optionalText(draft.rationale);
  if (draft.status === 'resolved') {
    if (!decision) {
      return { content: null, error: 'A resolved item requires a decision.' };
    }
    if (!rationale) {
      return { content: null, error: 'A resolved item requires a rationale.' };
    }
    if (confidence === null) {
      return { content: null, error: 'A resolved item requires confidence.' };
    }
    if (evidenceRefs.length === 0 && !evidenceAbsenceJustification) {
      return {
        content: null,
        error:
          'A resolved item requires evidence or an evidence-absence justification.',
      };
    }
  }

  return {
    content: {
      unknown,
      status: draft.status,
      anchor: {
        anchor_type: draft.anchorType,
        anchor_ref: anchorRef,
      },
      evidence_refs: evidenceRefs,
      alternatives: normalizedLines(draft.alternatives),
      decision,
      rationale,
      confidence,
      evidence_absence_justification: evidenceAbsenceJustification,
    },
    error: null,
  };
}

function errorMessage(error: unknown): string {
  if (error instanceof ResearchDecisionApiError) {
    const reason = error.details?.reason_code;
    return reason && reason !== error.message
      ? `${error.message} (${reason})`
      : error.message;
  }
  return error instanceof Error ? error.message : String(error);
}

function LegacyDecisions({
  decisions,
}: {
  decisions: readonly LegacyRefinementDecision[];
}) {
  const visible = decisions.filter(
    (decision) => typeof decision === 'string' && decision.trim(),
  );
  if (visible.length === 0) return null;

  return (
    <details
      className="rounded-lg border border-dashed border-gray-300 bg-gray-50 p-3 dark:border-gray-600 dark:bg-gray-900/30"
      data-testid="legacy-refinement-decisions"
    >
      <summary className="cursor-pointer text-xs font-semibold text-gray-700 dark:text-gray-200">
        Legacy refinement decisions ({visible.length})
      </summary>
      <p className="mt-2 text-[11px] text-gray-500 dark:text-gray-400">
        Historical read-only strings. They are separate from the immutable
        Research Decision Ledger and cannot be superseded here.
      </p>
      <ol className="mt-2 list-decimal space-y-1 pl-5 text-xs text-gray-600 dark:text-gray-300">
        {visible.map((decision, index) => (
          <li key={`${index}-${decision}`}>{decision}</li>
        ))}
      </ol>
    </details>
  );
}

function Editor({
  editor,
  error,
  submitting,
  onDraftChange,
  onCancel,
  onSubmit,
}: {
  editor: ResearchDecisionEditorState;
  error: string | null;
  submitting: boolean;
  onDraftChange: (patch: Partial<ResearchDecisionDraft>) => void;
  onCancel: () => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
}) {
  const isResolved = editor.draft.status === 'resolved';

  return (
    <form
      onSubmit={onSubmit}
      className="space-y-3 rounded-lg border border-violet-200 bg-violet-50/50 p-4 dark:border-violet-800 dark:bg-violet-950/20"
      data-testid="research-decision-editor"
    >
      <div className="flex items-start justify-between gap-3">
        <div>
          <h4 className="text-xs font-semibold uppercase tracking-wide text-violet-800 dark:text-violet-200">
            {editor.operation === 'append'
              ? 'Append a research thread'
              : 'Supersede the current thread head'}
          </h4>
          {editor.operation === 'supersede' && (
            <p className="mt-1 font-mono text-[10px] text-violet-600 dark:text-violet-300">
              ledger {editor.ledgerId} · expected head revision{' '}
              {editor.expectedHeadRevision} · predecessor{' '}
              {editor.predecessorEntryId}
            </p>
          )}
        </div>
        <button
          type="button"
          onClick={onCancel}
          aria-label="Close research decision editor"
          className="rounded p-1 text-gray-400 hover:bg-white hover:text-gray-600 dark:hover:bg-gray-800"
        >
          <X size={14} />
        </button>
      </div>

      <label className="block text-[11px] font-medium text-gray-700 dark:text-gray-200">
        Unknown or research question
        <textarea
          value={editor.draft.unknown}
          onChange={(event) => onDraftChange({ unknown: event.target.value })}
          rows={2}
          placeholder="What remains unknown?"
          className="mt-1 w-full rounded border border-gray-300 bg-white px-2.5 py-2 text-xs dark:border-gray-600 dark:bg-gray-800"
        />
      </label>

      <div className="grid gap-3 sm:grid-cols-3">
        <label className="text-[11px] font-medium text-gray-700 dark:text-gray-200">
          Status
          <select
            aria-label="Research decision status"
            value={editor.draft.status}
            onChange={(event) =>
              onDraftChange({
                status: event.target.value as ResearchDecisionStatus,
              })
            }
            className="mt-1 w-full rounded border border-gray-300 bg-white px-2 py-2 text-xs dark:border-gray-600 dark:bg-gray-800"
          >
            {(Object.keys(STATUS_LABELS) as ResearchDecisionStatus[]).map(
              (status) => (
                <option key={status} value={status}>
                  {STATUS_LABELS[status]}
                </option>
              ),
            )}
          </select>
        </label>
        <label className="text-[11px] font-medium text-gray-700 dark:text-gray-200">
          Anchor type
          <select
            aria-label="Research decision anchor type"
            value={editor.draft.anchorType}
            onChange={(event) =>
              onDraftChange({
                anchorType: event.target.value as ResearchDecisionAnchorType,
              })
            }
            className="mt-1 w-full rounded border border-gray-300 bg-white px-2 py-2 text-xs dark:border-gray-600 dark:bg-gray-800"
          >
            {(Object.keys(ANCHOR_LABELS) as ResearchDecisionAnchorType[]).map(
              (anchorType) => (
                <option key={anchorType} value={anchorType}>
                  {ANCHOR_LABELS[anchorType]}
                </option>
              ),
            )}
          </select>
        </label>
        <label className="text-[11px] font-medium text-gray-700 dark:text-gray-200">
          Stable anchor reference
          <input
            value={editor.draft.anchorRef}
            onChange={(event) => onDraftChange({ anchorRef: event.target.value })}
            placeholder="FR-1, AC-2, TR-3, Q6"
            className="mt-1 w-full rounded border border-gray-300 bg-white px-2 py-2 text-xs dark:border-gray-600 dark:bg-gray-800"
          />
        </label>
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <label className="text-[11px] font-medium text-gray-700 dark:text-gray-200">
          Evidence references
          <textarea
            value={editor.draft.evidenceRefs}
            onChange={(event) =>
              onDraftChange({ evidenceRefs: event.target.value })
            }
            rows={3}
            placeholder="One immutable reference per line"
            className="mt-1 w-full rounded border border-gray-300 bg-white px-2.5 py-2 text-xs dark:border-gray-600 dark:bg-gray-800"
          />
        </label>
        <label className="text-[11px] font-medium text-gray-700 dark:text-gray-200">
          Alternatives considered
          <textarea
            value={editor.draft.alternatives}
            onChange={(event) =>
              onDraftChange({ alternatives: event.target.value })
            }
            rows={3}
            placeholder="One alternative per line"
            className="mt-1 w-full rounded border border-gray-300 bg-white px-2.5 py-2 text-xs dark:border-gray-600 dark:bg-gray-800"
          />
        </label>
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <label className="text-[11px] font-medium text-gray-700 dark:text-gray-200">
          Decision {isResolved && <span className="text-red-500">*</span>}
          <textarea
            value={editor.draft.decision}
            onChange={(event) => onDraftChange({ decision: event.target.value })}
            rows={2}
            className="mt-1 w-full rounded border border-gray-300 bg-white px-2.5 py-2 text-xs dark:border-gray-600 dark:bg-gray-800"
          />
        </label>
        <label className="text-[11px] font-medium text-gray-700 dark:text-gray-200">
          Rationale {isResolved && <span className="text-red-500">*</span>}
          <textarea
            value={editor.draft.rationale}
            onChange={(event) => onDraftChange({ rationale: event.target.value })}
            rows={2}
            className="mt-1 w-full rounded border border-gray-300 bg-white px-2.5 py-2 text-xs dark:border-gray-600 dark:bg-gray-800"
          />
        </label>
      </div>

      <div className="grid gap-3 sm:grid-cols-[140px_minmax(0,1fr)]">
        <label className="text-[11px] font-medium text-gray-700 dark:text-gray-200">
          Confidence (0–1) {isResolved && <span className="text-red-500">*</span>}
          <input
            type="number"
            min="0"
            max="1"
            step="0.01"
            value={editor.draft.confidence}
            onChange={(event) =>
              onDraftChange({ confidence: event.target.value })
            }
            className="mt-1 w-full rounded border border-gray-300 bg-white px-2 py-2 text-xs dark:border-gray-600 dark:bg-gray-800"
          />
        </label>
        <label className="text-[11px] font-medium text-gray-700 dark:text-gray-200">
          Evidence-absence justification
          <input
            value={editor.draft.evidenceAbsenceJustification}
            onChange={(event) =>
              onDraftChange({
                evidenceAbsenceJustification: event.target.value,
              })
            }
            placeholder="Required for resolved status when no evidence refs exist"
            className="mt-1 w-full rounded border border-gray-300 bg-white px-2 py-2 text-xs dark:border-gray-600 dark:bg-gray-800"
          />
        </label>
      </div>

      {error && (
        <p
          className="rounded border border-red-200 bg-red-50 px-3 py-2 text-[11px] text-red-700 dark:border-red-800 dark:bg-red-950/30 dark:text-red-200"
          data-testid="research-decision-write-error"
        >
          {error}
        </p>
      )}

      <div className="flex justify-end gap-2">
        <button
          type="button"
          onClick={onCancel}
          disabled={submitting}
          className="rounded border border-gray-300 px-3 py-2 text-xs text-gray-600 hover:bg-white disabled:opacity-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800"
        >
          Cancel
        </button>
        <button
          type="submit"
          disabled={submitting}
          className="inline-flex items-center gap-1.5 rounded bg-violet-600 px-3 py-2 text-xs font-medium text-white hover:bg-violet-700 disabled:bg-gray-400"
        >
          {submitting ? (
            <Loader2 size={13} className="animate-spin" />
          ) : (
            <Send size={13} />
          )}
          {editor.operation === 'append' ? 'Append thread' : 'Supersede head'}
        </button>
      </div>
    </form>
  );
}

/**
 * Refinement modal integration:
 *
 * <ResearchDecisionTab
 *   boardId={refinement.board_id}
 *   refinementId={refinement.id}
 *   refinementStatus={refinement.status}
 *   refinementArchived={refinement.archived}
 *   legacyDecisions={refinement.decisions}
 *   onRefinementVersionChanged={(version) => updateHostVersion(version)}
 * />
 *
 * The host only owns tab selection and the displayed Refinement version. This
 * component owns RDL permissions, fetching, filters, pagination, and writes.
 */
export function ResearchDecisionPanel({
  boardId,
  refinementId,
  refinementStatus,
  refinementArchived = false,
  legacyDecisions = [],
  className = '',
  onRefinementVersionChanged,
}: ResearchDecisionPanelProps) {
  const api = useResearchDecisionsApi();
  const apiRef = useRef(api);
  apiRef.current = api;
  const versionCallbackRef = useRef(onRefinementVersionChanged);
  versionCallbackRef.current = onRefinementVersionChanged;
  const permissions = usePermissions(boardId);
  const canRead = permissions.has(READ_PERMISSION);
  const canAppend = permissions.has(APPEND_PERMISSION);
  const canWrite =
    canAppend && refinementStatus === 'draft' && !refinementArchived;

  const [page, setPage] = useState<ResearchDecisionPage | null>(null);
  const [offset, setOffset] = useState(0);
  const [limit, setLimit] = useState<ResearchDecisionPageSize>(25);
  const [statusFilter, setStatusFilter] =
    useState<ResearchDecisionStatus | null>(null);
  const [ledgerFilterDraft, setLedgerFilterDraft] = useState('');
  const [ledgerFilter, setLedgerFilter] = useState<string | null>(null);
  const [reloadRevision, setReloadRevision] = useState(0);
  const [loading, setLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [editor, setEditor] = useState<ResearchDecisionEditorState | null>(null);
  const [writeError, setWriteError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [resolvingLedger, setResolvingLedger] = useState<string | null>(null);
  const idempotencyKeysByFingerprintRef = useRef(new Map<string, string>());

  useEffect(() => {
    setOffset(0);
    setLedgerFilter(null);
    setLedgerFilterDraft('');
    setStatusFilter(null);
    setEditor(null);
    setWriteError(null);
    idempotencyKeysByFingerprintRef.current.clear();
  }, [refinementId]);

  useEffect(() => {
    if (!canRead) {
      setPage(null);
      setListError(null);
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setListError(null);
    apiRef.current
      .listResearchDecisions(refinementId, {
        offset,
        limit,
        ledgerId: ledgerFilter,
        status: statusFilter,
        signal: controller.signal,
      })
      .then((result) => {
        if (!controller.signal.aborted) setPage(result);
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted) {
          setPage(null);
          setListError(errorMessage(error));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [
    canRead,
    ledgerFilter,
    limit,
    offset,
    refinementId,
    reloadRevision,
    statusFilter,
  ]);

  const closeEditor = () => {
    setEditor(null);
    setWriteError(null);
  };

  const openAppend = () => {
    if (!canWrite) return;
    setEditor({
      operation: 'append',
      draft: emptyDraft(),
      ledgerId: null,
      predecessorEntryId: null,
      expectedHeadRevision: 0,
    });
    setWriteError(null);
  };

  const openSupersede = async (ledgerId: string) => {
    if (!canWrite || resolvingLedger) return;
    setResolvingLedger(ledgerId);
    setWriteError(null);
    try {
      const head = await apiRef.current.resolveResearchDecisionHead(
        refinementId,
        ledgerId,
      );
      setEditor({
        operation: 'supersede',
        draft: draftFromEntry(head.entry),
        ledgerId: head.entry.ledger_id,
        predecessorEntryId: head.entry.id,
        expectedHeadRevision: head.headRevision,
      });
    } catch (error: unknown) {
      toast.error(errorMessage(error));
    } finally {
      setResolvingLedger(null);
    }
  };

  const updateDraft = (patch: Partial<ResearchDecisionDraft>) => {
    setEditor((current) =>
      current
        ? { ...current, draft: { ...current.draft, ...patch } }
        : current,
    );
    setWriteError(null);
  };

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!editor || !canWrite || submitting) return;
    const parsed = contentFromDraft(editor.draft);
    if (!parsed.content) {
      setWriteError(parsed.error);
      return;
    }

    const intent =
      editor.operation === 'append'
        ? {
            operation: 'append' as const,
            expected_head_revision: 0 as const,
            entry: parsed.content,
          }
        : {
            operation: 'supersede' as const,
            expected_head_revision: editor.expectedHeadRevision,
            ledger_id: editor.ledgerId ?? '',
            supersedes_entry_id: editor.predecessorEntryId ?? '',
            entry: parsed.content,
          };
    const fingerprint = JSON.stringify(intent);
    const idempotencyKey = idempotencyKeyForFingerprint(
      idempotencyKeysByFingerprintRef.current,
      fingerprint,
    );
    setSubmitting(true);
    setWriteError(null);

    try {
      const result =
        intent.operation === 'append'
          ? await apiRef.current.appendResearchDecision(refinementId, {
              ...intent,
              idempotency_key: idempotencyKey,
            })
          : await apiRef.current.supersedeResearchDecision(refinementId, {
              ...intent,
              idempotency_key: idempotencyKey,
            });
      versionCallbackRef.current?.(result.refinement_version);
      toast.success(
        result.replayed
          ? 'Research decision replay confirmed'
          : editor.operation === 'append'
            ? 'Research thread appended'
            : 'Research thread head superseded',
      );
      closeEditor();
      setOffset(0);
      setReloadRevision((current) => current + 1);
    } catch (error: unknown) {
      if (
        editor.operation === 'supersede' &&
        error instanceof ResearchDecisionApiError &&
        error.status === 409 &&
        error.retryable &&
        (
          error.code === 'research_decision_head_revision_conflict'
          || error.details?.reason_code === 'research_decision_head_revision_conflict'
        ) &&
        editor.ledgerId
      ) {
        try {
          const latest = await apiRef.current.resolveResearchDecisionHead(
            refinementId,
            editor.ledgerId,
          );
          setEditor((current) =>
            current
              ? {
                  ...current,
                  ledgerId: latest.entry.ledger_id,
                  predecessorEntryId: latest.entry.id,
                  expectedHeadRevision: latest.headRevision,
                }
              : current,
          );
          setWriteError(
            'The thread changed concurrently. The latest CAS head was loaded; review your draft and submit again.',
          );
          setReloadRevision((current) => current + 1);
        } catch (refreshError: unknown) {
          setWriteError(
            `${errorMessage(error)}; failed to refresh the current head: ${errorMessage(
              refreshError,
            )}`,
          );
        }
      } else {
        setWriteError(errorMessage(error));
      }
    } finally {
      setSubmitting(false);
    }
  };

  const legacyBlock = <LegacyDecisions decisions={legacyDecisions ?? []} />;

  if (permissions.isLoading) {
    return (
      <div className={`space-y-3 ${className}`}>
        <section className="flex items-center gap-2 rounded-lg border border-gray-200 p-4 text-xs text-gray-500 dark:border-gray-700 dark:text-gray-400">
          <Loader2 size={13} className="animate-spin" />
          Loading Research Decision Ledger permissions…
        </section>
        {legacyBlock}
      </div>
    );
  }

  if (!canRead) {
    return (
      <div className={`space-y-3 ${className}`}>
        <section
          className="rounded-lg border border-gray-200 p-4 text-xs text-gray-500 dark:border-gray-700 dark:text-gray-400"
          data-testid="research-decision-read-denied"
        >
          <span className="flex items-center gap-1.5 font-medium">
            <Lock size={13} />
            The `{READ_PERMISSION}` permission is required to view the immutable
            ledger.
          </span>
          {permissions.ownerReviewRequired && (
            <p className="mt-1 text-amber-600 dark:text-amber-300">
              Permission lineage requires owner review.
            </p>
          )}
        </section>
        {legacyBlock}
      </div>
    );
  }

  return (
    <div className={`space-y-4 ${className}`} data-testid="research-decision-panel">
      <section className="space-y-4 rounded-lg border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-900/40">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h3 className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-gray-800 dark:text-gray-100">
              <GitBranch size={14} />
              Research Decision Ledger
            </h3>
            <p className="mt-1 text-[11px] text-gray-500 dark:text-gray-400">
              Immutable research threads anchored to stable Refinement entities.
              Supersede appends a new CAS-protected head; it never edits history.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setReloadRevision((current) => current + 1)}
              aria-label="Refresh research decisions"
              disabled={loading}
              className="rounded p-1.5 text-gray-400 hover:bg-gray-100 hover:text-gray-600 disabled:opacity-40 dark:hover:bg-gray-800"
            >
              <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
            </button>
            {canWrite && (
              <button
                type="button"
                onClick={openAppend}
                className="inline-flex items-center gap-1.5 rounded bg-violet-600 px-3 py-2 text-xs font-medium text-white hover:bg-violet-700"
              >
                <Plus size={13} />
                New research thread
              </button>
            )}
          </div>
        </div>

        {!canAppend && (
          <p className="rounded bg-gray-50 px-3 py-2 text-[11px] text-gray-500 dark:bg-gray-800/60 dark:text-gray-400">
            Read-only: `{APPEND_PERMISSION}` is required to append or supersede.
          </p>
        )}
        {canAppend && !canWrite && (
          <p className="rounded bg-amber-50 px-3 py-2 text-[11px] text-amber-700 dark:bg-amber-950/30 dark:text-amber-200">
            Research threads can only be changed while the Refinement is an
            active Draft.
          </p>
        )}

        {editor && canWrite && (
          <Editor
            editor={editor}
            error={writeError}
            submitting={submitting}
            onDraftChange={updateDraft}
            onCancel={closeEditor}
            onSubmit={(event) => void handleSubmit(event)}
          />
        )}

        <div className="flex flex-wrap items-end gap-2 rounded-lg bg-gray-50 p-3 dark:bg-gray-800/50">
          <form
            className="flex min-w-[240px] flex-1 items-end gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              setLedgerFilter(ledgerFilterDraft.trim() || null);
              setOffset(0);
            }}
          >
            <label className="min-w-0 flex-1 text-[10px] font-medium uppercase text-gray-500 dark:text-gray-400">
              Ledger id
              <input
                value={ledgerFilterDraft}
                onChange={(event) => setLedgerFilterDraft(event.target.value)}
                placeholder="Filter one immutable thread"
                className="mt-1 w-full rounded border border-gray-300 bg-white px-2 py-1.5 text-xs normal-case dark:border-gray-600 dark:bg-gray-900"
              />
            </label>
            <button
              type="submit"
              aria-label="Apply research decision filters"
              className="rounded border border-gray-300 p-1.5 text-gray-500 hover:bg-white dark:border-gray-600 dark:hover:bg-gray-900"
            >
              <Filter size={14} />
            </button>
            {ledgerFilter && (
              <button
                type="button"
                onClick={() => {
                  setLedgerFilter(null);
                  setLedgerFilterDraft('');
                  setOffset(0);
                }}
                className="rounded px-2 py-1.5 text-[10px] text-gray-500 hover:bg-white dark:hover:bg-gray-900"
              >
                Clear
              </button>
            )}
          </form>
          <label className="text-[10px] font-medium uppercase text-gray-500 dark:text-gray-400">
            Status
            <select
              aria-label="Filter research decisions by status"
              value={statusFilter ?? ''}
              onChange={(event) => {
                setStatusFilter(
                  (event.target.value || null) as ResearchDecisionStatus | null,
                );
                setOffset(0);
              }}
              className="mt-1 block rounded border border-gray-300 bg-white px-2 py-1.5 text-xs normal-case dark:border-gray-600 dark:bg-gray-900"
            >
              <option value="">All statuses</option>
              {(Object.keys(STATUS_LABELS) as ResearchDecisionStatus[]).map(
                (status) => (
                  <option key={status} value={status}>
                    {STATUS_LABELS[status]}
                  </option>
                ),
              )}
            </select>
          </label>
          <label className="text-[10px] font-medium uppercase text-gray-500 dark:text-gray-400">
            Page size
            <select
              aria-label="Research decision page size"
              value={limit}
              onChange={(event) => {
                setLimit(Number(event.target.value) as ResearchDecisionPageSize);
                setOffset(0);
              }}
              className="mt-1 block rounded border border-gray-300 bg-white px-2 py-1.5 text-xs normal-case dark:border-gray-600 dark:bg-gray-900"
            >
              {RESEARCH_DECISION_PAGE_SIZES.map((size) => (
                <option key={size} value={size}>
                  {size}
                </option>
              ))}
            </select>
          </label>
        </div>

        {listError && (
          <p
            className="rounded border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700 dark:border-red-800 dark:bg-red-950/30 dark:text-red-200"
            data-testid="research-decision-list-error"
          >
            {listError}
          </p>
        )}

        {loading && !page ? (
          <div className="flex items-center gap-2 py-6 text-xs text-gray-500">
            <Loader2 size={14} className="animate-spin" />
            Loading immutable entries…
          </div>
        ) : page?.items.length === 0 ? (
          <p className="py-6 text-center text-xs italic text-gray-400">
            No research decisions match these filters.
          </p>
        ) : (
          <div className="space-y-3">
            {page?.items.map((entry) => (
              <article
                key={entry.id}
                className="rounded-lg border border-gray-200 p-3 dark:border-gray-700"
                data-testid={`research-decision-entry-${entry.id}`}
              >
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span
                        className={`rounded px-1.5 py-0.5 text-[9px] font-bold uppercase ${STATUS_STYLES[entry.content.status]}`}
                      >
                        {STATUS_LABELS[entry.content.status]}
                      </span>
                      <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[9px] text-gray-600 dark:bg-gray-800 dark:text-gray-300">
                        immutable
                      </span>
                      <span className="font-mono text-[9px] text-gray-400">
                        Refinement v{entry.refinement_version}
                      </span>
                    </div>
                    <h4 className="mt-2 text-sm font-semibold text-gray-900 dark:text-gray-100">
                      {entry.content.unknown}
                    </h4>
                    <p className="mt-1 text-[10px] text-gray-500 dark:text-gray-400">
                      {ANCHOR_LABELS[entry.content.anchor.anchor_type]} ·{' '}
                      <span className="font-mono">
                        {entry.content.anchor.anchor_ref}
                      </span>
                    </p>
                  </div>
                  {canWrite && (
                    <button
                      type="button"
                      onClick={() => void openSupersede(entry.ledger_id)}
                      disabled={resolvingLedger !== null || submitting}
                      aria-label={`Supersede current head of ledger ${entry.ledger_id}`}
                      className="inline-flex items-center gap-1 rounded border border-violet-200 px-2 py-1.5 text-[10px] font-medium text-violet-700 hover:bg-violet-50 disabled:opacity-50 dark:border-violet-700 dark:text-violet-200 dark:hover:bg-violet-950/30"
                    >
                      {resolvingLedger === entry.ledger_id ? (
                        <Loader2 size={11} className="animate-spin" />
                      ) : (
                        <GitBranch size={11} />
                      )}
                      Supersede thread head
                    </button>
                  )}
                </div>

                {(entry.content.decision || entry.content.rationale) && (
                  <div className="mt-3 grid gap-2 sm:grid-cols-2">
                    {entry.content.decision && (
                      <div className="rounded bg-green-50 p-2 dark:bg-green-950/20">
                        <p className="text-[9px] font-bold uppercase text-green-700 dark:text-green-300">
                          Decision
                        </p>
                        <p className="mt-1 text-xs text-gray-700 dark:text-gray-200">
                          {entry.content.decision}
                        </p>
                      </div>
                    )}
                    {entry.content.rationale && (
                      <div className="rounded bg-gray-50 p-2 dark:bg-gray-800/60">
                        <p className="text-[9px] font-bold uppercase text-gray-500">
                          Rationale
                        </p>
                        <p className="mt-1 text-xs text-gray-700 dark:text-gray-200">
                          {entry.content.rationale}
                        </p>
                      </div>
                    )}
                  </div>
                )}

                {(entry.content.evidence_refs.length > 0 ||
                  entry.content.alternatives.length > 0) && (
                  <div className="mt-2 flex flex-wrap gap-1">
                    {entry.content.evidence_refs.map((reference) => (
                      <span
                        key={`evidence-${reference}`}
                        className="rounded bg-blue-50 px-1.5 py-0.5 font-mono text-[9px] text-blue-700 dark:bg-blue-950/30 dark:text-blue-200"
                      >
                        evidence: {reference}
                      </span>
                    ))}
                    {entry.content.alternatives.map((alternative) => (
                      <span
                        key={`alternative-${alternative}`}
                        className="rounded bg-gray-100 px-1.5 py-0.5 text-[9px] text-gray-600 dark:bg-gray-800 dark:text-gray-300"
                      >
                        alternative: {alternative}
                      </span>
                    ))}
                  </div>
                )}

                <div className="mt-3 grid gap-1 border-t border-gray-100 pt-2 font-mono text-[9px] text-gray-400 dark:border-gray-800 sm:grid-cols-2">
                  <span>ledger: {entry.ledger_id}</span>
                  <span>entry: {entry.id}</span>
                  <span>
                    predecessor: {entry.predecessor_entry_id ?? 'root'}
                  </span>
                  <span>
                    {new Date(entry.created_at).toLocaleString()} ·{' '}
                    {entry.created_by}
                  </span>
                  <span className="sm:col-span-2">
                    digest: {entry.content_digest}
                  </span>
                </div>
              </article>
            ))}
          </div>
        )}

        {page && (
          <div className="flex flex-wrap items-center justify-between gap-2 border-t border-gray-200 pt-3 text-[10px] text-gray-500 dark:border-gray-700 dark:text-gray-400">
            <span>
              {page.total_filtered === 0 ? 0 : page.offset + 1}–
              {Math.min(page.offset + page.limit, page.total_filtered)} of{' '}
              {page.total_filtered} filtered · {page.total_overall} total
            </span>
            <div className="flex items-center gap-1">
              <button
                type="button"
                onClick={() => setOffset(Math.max(0, offset - limit))}
                disabled={offset === 0 || loading}
                aria-label="Previous research decision page"
                className="rounded p-1 text-gray-500 hover:bg-gray-100 disabled:opacity-30 dark:hover:bg-gray-800"
              >
                <ChevronLeft size={14} />
              </button>
              <button
                type="button"
                onClick={() => setOffset(offset + limit)}
                disabled={
                  loading || offset + limit >= page.total_filtered
                }
                aria-label="Next research decision page"
                className="rounded p-1 text-gray-500 hover:bg-gray-100 disabled:opacity-30 dark:hover:bg-gray-800"
              >
                <ChevronRight size={14} />
              </button>
            </div>
          </div>
        )}
      </section>

      {legacyBlock}
    </div>
  );
}

/** Semantic alias for direct use as a Refinement modal tab body. */
export const ResearchDecisionTab = ResearchDecisionPanel;
