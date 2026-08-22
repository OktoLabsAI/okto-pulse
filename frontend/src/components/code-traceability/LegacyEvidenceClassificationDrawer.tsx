import {
  AlertTriangle,
  ArrowLeft,
  Check,
  ChevronRight,
  RefreshCw,
  ShieldCheck,
  X,
} from 'lucide-react';
import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from 'react';
import { createPortal } from 'react-dom';

import { useDialogFocusTrap } from '@/hooks/useDialogFocusTrap';
import { useEscapeToClose } from '@/hooks/useEscapeToClose';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import { LegacyEvidenceClassificationConflictError } from '@/services/api';
import type {
  AuthoredCodeEvidenceSourceRole,
  CodeTraceabilityEvidence,
  LegacyEvidenceClassificationBatchRequest,
  LegacyEvidenceClassificationBatchResult,
  SourceContextClassificationInputV2,
  SourceContextEvidenceItemV2,
} from '@/types';

import {
  buildLegacyEvidenceClassificationIntent,
  codeEvidenceBaselinePresenceLabel,
  codeEvidenceSourceRoleLabel,
  createLegacyEvidenceClassificationDraft,
  validateLegacyEvidenceClassificationDrafts,
  type LegacyEvidenceClassificationDraft,
  type LegacyEvidenceClassificationDraftIssue,
} from './sourceContextPresentation';
import {
  createLegacyClassificationIntentStore,
  type LegacyClassificationIntentStore,
  type ReviewedLegacyClassificationBatch,
} from './LegacyClassificationIntentStore';

export interface LegacyEvidenceClassificationSnapshot {
  classificationInputs: readonly SourceContextClassificationInputV2[];
  effectiveItems: readonly SourceContextEvidenceItemV2[];
  evidence: readonly CodeTraceabilityEvidence[];
}

export type ApplyLegacyEvidenceClassificationBatch = (
  request: LegacyEvidenceClassificationBatchRequest,
  signal: AbortSignal,
) => Promise<LegacyEvidenceClassificationBatchResult>;

export type RefetchLegacyEvidenceClassificationSnapshot = (
  signal: AbortSignal,
) => Promise<LegacyEvidenceClassificationSnapshot>;

export interface LegacyEvidenceClassificationDrawerProps {
  snapshot: LegacyEvidenceClassificationSnapshot;
  canClassify: boolean;
  opener?: HTMLElement | null;
  focusFallback?: HTMLElement | null;
  onClose: () => void;
  onApplyBatch: ApplyLegacyEvidenceClassificationBatch;
  onCanonicalRefetch: RefetchLegacyEvidenceClassificationSnapshot;
  onApplied?: (
    result: LegacyEvidenceClassificationBatchResult,
  ) => void | Promise<void>;
  createIdempotencyKey?: () => string;
}

type DrawerStep = 'classify' | 'review';

type SubmissionFailure =
  | { kind: 'network_ambiguous'; message: string }
  | { kind: 'canonical_conflict'; message: string; refreshUsed: boolean }
  | { kind: 'idempotency_conflict'; message: string }
  | { kind: 'forbidden'; message: string }
  | { kind: 'not_found'; message: string }
  | { kind: 'validation'; message: string }
  | { kind: 'deterministic'; message: string }
  | { kind: 'applied_refresh_failed'; message: string };

interface ValidationFocusTarget {
  evidenceId: string | null;
  field: LegacyEvidenceClassificationDraftIssue['field'] | 'batch';
}

const AUTHORED_ROLES: readonly AuthoredCodeEvidenceSourceRole[] = [
  'current_implementation',
  'existing_scaffold',
  'existing_constraint',
  'reference_pattern',
];

const SOURCE_ROLE_DESCRIPTIONS: Readonly<Record<AuthoredCodeEvidenceSourceRole, string>> = {
  current_implementation: 'Current behavior that already delivers the scope.',
  existing_scaffold: 'Reusable structure that does not prove delivery.',
  existing_constraint: 'A boundary the solution must respect.',
  reference_pattern: 'A comparison that does not prove this scope exists.',
};

function defaultIdempotencyKey(): string {
  const suffix = typeof globalThis.crypto?.randomUUID === 'function'
    ? globalThis.crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `legacy-classification-${suffix}`;
}

function draftFromCanonical(
  input: SourceContextClassificationInputV2,
  effectiveItem: SourceContextEvidenceItemV2 | null,
): LegacyEvidenceClassificationDraft {
  return createLegacyEvidenceClassificationDraft(input, effectiveItem);
}

function draftsFromSnapshot(
  snapshot: LegacyEvidenceClassificationSnapshot,
): LegacyEvidenceClassificationDraft[] {
  const effectiveById = new Map(
    snapshot.effectiveItems.map((item) => [item.evidence_id, item]),
  );
  return snapshot.classificationInputs.map((input) => draftFromCanonical(
    input,
    effectiveById.get(input.evidence_id) ?? null,
  ));
}

function reconcileDrafts(
  current: readonly LegacyEvidenceClassificationDraft[],
  snapshot: LegacyEvidenceClassificationSnapshot,
): LegacyEvidenceClassificationDraft[] {
  const currentById = new Map(current.map((draft) => [draft.evidence_id, draft]));
  const effectiveById = new Map(
    snapshot.effectiveItems.map((item) => [item.evidence_id, item]),
  );
  return snapshot.classificationInputs.map((input) => {
    const canonical = draftFromCanonical(
      input,
      effectiveById.get(input.evidence_id) ?? null,
    );
    const draft = currentById.get(input.evidence_id);
    if (!draft) return canonical;
    return {
      ...draft,
      expected_evidence_payload_sha256: canonical.expected_evidence_payload_sha256,
      expected_classification_revision: canonical.expected_classification_revision,
      baseline_presence: canonical.baseline_presence,
      baseline_workspace_state_id: canonical.baseline_workspace_state_id,
      provenance_note_required: canonical.provenance_note_required,
      provenance_note: canonical.provenance_note_required
        ? draft.provenance_note || canonical.provenance_note
        : canonical.provenance_note,
    };
  });
}

function errorMessage(error: unknown): string {
  if (error instanceof Error && error.message.trim()) return error.message;
  return 'The classification request could not be confirmed.';
}

function shortOrigin(evidence: CodeTraceabilityEvidence | undefined): string {
  return evidence?.relative_path?.trim() || 'Not included';
}

const DRAFT_FIELDS = new Set<LegacyEvidenceClassificationDraftIssue['field']>([
  'items',
  'evidence_id',
  'expected_evidence_payload_sha256',
  'expected_classification_revision',
  'source_role',
  'relevance_summary',
  'scope_relation',
  'source_origin',
  'interpretation_limit',
  'baseline_workspace_state_id',
  'provenance_note',
  'justification',
]);

function validationMessage(value: unknown, fallback: string): string {
  if (typeof value === 'string' && value.trim()) return value.trim();
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    if (typeof record.msg === 'string' && record.msg.trim()) return record.msg.trim();
    if (typeof record.message === 'string' && record.message.trim()) return record.message.trim();
  }
  return fallback;
}

function validationLocation(value: unknown): readonly (string | number)[] {
  if (!value || typeof value !== 'object') return [];
  const record = value as Record<string, unknown>;
  if (Array.isArray(record.loc)) return record.loc.filter(
    (part): part is string | number => typeof part === 'string' || typeof part === 'number',
  );
  if (typeof record.field === 'string') return record.field.split(/[.[\]]+/u).filter(Boolean);
  return [];
}

function issueFromServerEntry(
  value: unknown,
  reviewed: ReviewedLegacyClassificationBatch,
  fallbackMessage: string,
): LegacyEvidenceClassificationDraftIssue {
  const location = validationLocation(value);
  const normalized = location.map((part) => String(part));
  const itemMarker = normalized.lastIndexOf('items');
  const itemIndex = itemMarker >= 0 ? Number(normalized[itemMarker + 1]) : Number.NaN;
  const evidenceId = Number.isInteger(itemIndex)
    ? reviewed.request.items[itemIndex]?.evidence_id ?? null
    : null;
  const rawField = [...normalized].reverse().find((part) => DRAFT_FIELDS.has(
    part === 'workspace_state_id'
      ? 'baseline_workspace_state_id'
      : part as LegacyEvidenceClassificationDraftIssue['field'],
  ));
  const field = rawField === 'workspace_state_id'
    ? 'baseline_workspace_state_id'
    : rawField && DRAFT_FIELDS.has(rawField as LegacyEvidenceClassificationDraftIssue['field'])
      ? rawField as LegacyEvidenceClassificationDraftIssue['field']
      : 'items';
  return {
    evidenceId: field === 'justification' || field === 'items' ? null : evidenceId,
    field,
    code: 'invalid',
    message: validationMessage(value, fallbackMessage),
  };
}

function serverValidationIssues(
  error: AuthenticatedFetchError,
  reviewed: ReviewedLegacyClassificationBatch,
): LegacyEvidenceClassificationDraftIssue[] {
  const details = error.details;
  let entries: unknown[] = [];
  if (Array.isArray(details)) entries = details;
  else if (details && typeof details === 'object') {
    const record = details as Record<string, unknown>;
    const listed = record.errors ?? record.detail ?? record.violations;
    if (Array.isArray(listed)) entries = listed;
    else if (record.field_errors && typeof record.field_errors === 'object') {
      entries = Object.entries(record.field_errors as Record<string, unknown>).map(
        ([field, message]) => ({ field, message }),
      );
    }
  }
  if (entries.length === 0) entries = [{ field: 'items', message: error.message }];
  return entries.map((entry) => issueFromServerEntry(entry, reviewed, error.message));
}

function firstValidationFocus(
  issues: readonly LegacyEvidenceClassificationDraftIssue[],
): ValidationFocusTarget {
  // In a focused batch, surface the first owning item before a batch-level
  // justification issue so the relevant form is mounted when focus moves.
  const first = issues.find((issue) => issue.evidenceId !== null) ?? issues[0];
  return first
    ? { evidenceId: first.evidenceId, field: first.field }
    : { evidenceId: null, field: 'batch' };
}

function submissionFailureTitle(failure: SubmissionFailure): string {
  switch (failure.kind) {
    case 'network_ambiguous':
      return 'Submission outcome not confirmed';
    case 'canonical_conflict':
      return 'The canonical Evidence changed after review';
    case 'idempotency_conflict':
      return 'This submission no longer matches its reviewed key';
    case 'forbidden':
      return 'Permission to classify is no longer available';
    case 'not_found':
      return 'Evidence is no longer available';
    case 'validation':
      return 'Review the highlighted classification fields';
    case 'deterministic':
      return 'The classification was not accepted';
    case 'applied_refresh_failed':
      return 'Classification applied; refresh failed';
  }
}

function needsInterpretationLimit(
  role: LegacyEvidenceClassificationDraft['source_role'],
): boolean {
  return role === 'existing_scaffold' || role === 'reference_pattern';
}

function useMaskOpenerDialog(
  active: boolean,
  opener?: HTMLElement | null,
) {
  useEffect(() => {
    if (!active) return undefined;
    const activeElement = opener
      ?? (document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null);
    const parent = activeElement?.closest<HTMLElement>(
      '[role="dialog"], [role="alertdialog"]',
    ) ?? null;
    if (!parent) return undefined;
    const previousAriaHidden = parent.getAttribute('aria-hidden');
    const previousAriaModal = parent.getAttribute('aria-modal');
    parent.setAttribute('aria-hidden', 'true');
    parent.removeAttribute('aria-modal');
    return () => {
      if (previousAriaHidden === null) parent.removeAttribute('aria-hidden');
      else parent.setAttribute('aria-hidden', previousAriaHidden);
      if (previousAriaModal === null) parent.removeAttribute('aria-modal');
      else parent.setAttribute('aria-modal', previousAriaModal);
    };
  }, [active, opener]);
}

function useLockDocumentScroll(active: boolean) {
  useEffect(() => {
    if (!active) return undefined;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [active]);
}

function FieldIssue({
  issues,
  evidenceId,
  field,
}: {
  issues: readonly LegacyEvidenceClassificationDraftIssue[];
  evidenceId: string | null;
  field: LegacyEvidenceClassificationDraftIssue['field'];
}) {
  const issue = issues.find(
    (candidate) => candidate.evidenceId === evidenceId && candidate.field === field,
  );
  if (!issue) return null;
  return (
    <span className="mt-1 block text-[11px] text-red-600 dark:text-red-300" role="alert">
      {issue.message}
    </span>
  );
}

function CanonicalBaseline({
  draft,
  issues,
}: {
  draft: LegacyEvidenceClassificationDraft;
  issues: readonly LegacyEvidenceClassificationDraftIssue[];
}) {
  return (
    <section className="rounded-lg border border-gray-200 bg-gray-50/70 p-3 dark:border-gray-700 dark:bg-gray-900/30">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h4 className="text-xs font-semibold text-gray-800 dark:text-gray-100">
          Investigation baseline
        </h4>
        <span className="rounded-md bg-gray-200 px-2 py-0.5 text-[10px] font-semibold text-gray-600 dark:bg-gray-700 dark:text-gray-300">
          Read-only
        </span>
      </div>
      <p className="mt-0.5 text-[11px] leading-4 text-gray-500 dark:text-gray-400">
        The source state and workspace come from the investigation and cannot be changed here.
      </p>
      <dl className="mt-2 grid gap-2 text-xs sm:grid-cols-2">
        <div className="rounded-lg border border-gray-200 bg-white p-2.5 dark:border-gray-700 dark:bg-gray-900">
          <dt className="text-[10px] font-semibold uppercase tracking-wide text-gray-400">
            Source state
          </dt>
          <dd className="mt-0.5 text-gray-700 dark:text-gray-200">
            {codeEvidenceBaselinePresenceLabel(draft.baseline_presence)}
          </dd>
        </div>
        <div
          data-evidence-id={draft.evidence_id}
          data-validation-field="baseline_workspace_state_id"
          tabIndex={-1}
          className="rounded-lg border border-gray-200 bg-white p-2.5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-500 dark:border-gray-700 dark:bg-gray-900"
        >
          <dt className="text-[10px] font-semibold uppercase tracking-wide text-gray-400">
            Observed workspace
          </dt>
          <dd className="mt-0.5 text-gray-700 dark:text-gray-200">
            Investigation baseline
          </dd>
          <FieldIssue
            issues={issues}
            evidenceId={draft.evidence_id}
            field="baseline_workspace_state_id"
          />
        </div>
      </dl>
    </section>
  );
}

function TechnicalDetails({
  evidenceId,
  workspaceStateId,
  payloadSha256,
  classificationRevision,
  issues = [],
}: {
  evidenceId: string;
  workspaceStateId: string;
  payloadSha256: string;
  classificationRevision: number;
  issues?: readonly LegacyEvidenceClassificationDraftIssue[];
}) {
  return (
    <details className="rounded-lg border border-gray-100 px-3 py-2 text-xs dark:border-gray-700">
      <summary className="cursor-pointer font-medium text-gray-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500 dark:text-gray-400">
        Technical details
      </summary>
      <dl className="mt-2 grid gap-2 sm:grid-cols-2">
        <div
          data-evidence-id={evidenceId}
          data-validation-field="evidence_id"
          tabIndex={-1}
          className="focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-500"
        >
          <dt className="text-[10px] uppercase tracking-wide text-gray-400">Evidence ID</dt>
          <dd className="break-all font-mono text-[10px] text-gray-600 dark:text-gray-300">
            {evidenceId}
          </dd>
          <FieldIssue issues={issues} evidenceId={evidenceId} field="evidence_id" />
        </div>
        <div
          data-evidence-id={evidenceId}
          data-validation-field="baseline_workspace_state_id"
          tabIndex={-1}
          className="focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-500"
        >
          <dt className="text-[10px] uppercase tracking-wide text-gray-400">Workspace state ID</dt>
          <dd className="break-all font-mono text-[10px] text-gray-600 dark:text-gray-300">
            {workspaceStateId}
          </dd>
        </div>
        <div
          data-evidence-id={evidenceId}
          data-validation-field="expected_evidence_payload_sha256"
          tabIndex={-1}
          className="focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-500"
        >
          <dt className="text-[10px] uppercase tracking-wide text-gray-400">Payload SHA-256</dt>
          <dd className="break-all font-mono text-[10px] text-gray-600 dark:text-gray-300">
            {payloadSha256}
          </dd>
          <FieldIssue
            issues={issues}
            evidenceId={evidenceId}
            field="expected_evidence_payload_sha256"
          />
        </div>
        <div
          data-evidence-id={evidenceId}
          data-validation-field="expected_classification_revision"
          tabIndex={-1}
          className="focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-500"
        >
          <dt className="text-[10px] uppercase tracking-wide text-gray-400">Classification revision</dt>
          <dd className="font-mono text-[11px] text-gray-600 dark:text-gray-300">
            {classificationRevision}
          </dd>
          <FieldIssue
            issues={issues}
            evidenceId={evidenceId}
            field="expected_classification_revision"
          />
        </div>
      </dl>
    </details>
  );
}

function ClassifyStep({
  snapshot,
  drafts,
  justification,
  issues,
  showIssues,
  activeEvidenceId,
  onDraftsChange,
  onJustificationChange,
  onActiveEvidenceChange,
}: {
  snapshot: LegacyEvidenceClassificationSnapshot;
  drafts: LegacyEvidenceClassificationDraft[];
  justification: string;
  issues: readonly LegacyEvidenceClassificationDraftIssue[];
  showIssues: boolean;
  activeEvidenceId: string | null;
  onDraftsChange: (drafts: LegacyEvidenceClassificationDraft[]) => void;
  onJustificationChange: (value: string) => void;
  onActiveEvidenceChange: (evidenceId: string) => void;
}) {
  const evidenceById = useMemo(
    () => new Map(snapshot.evidence.map((item) => [item.id, item])),
    [snapshot.evidence],
  );
  const activeIndex = Math.max(0, drafts.findIndex(
    (draft) => draft.evidence_id === activeEvidenceId,
  ));
  const activeDraft = drafts[activeIndex];

  const updateDraft = (
    index: number,
    update: Partial<LegacyEvidenceClassificationDraft>,
  ) => {
    onDraftsChange(drafts.map((draft, candidate) => (
      candidate === index ? { ...draft, ...update } : draft
    )));
  };

  return (
    <div
      className="space-y-4"
      data-testid="legacy-classification-classify-step"
      data-validation-field="batch"
      tabIndex={-1}
    >
      <FieldIssue
        issues={showIssues ? issues : []}
        evidenceId={null}
        field="items"
      />
      {drafts.length === 0 && (
        <p className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800 dark:border-amber-800 dark:bg-amber-950/30 dark:text-amber-200" role="alert">
          No canonical classification inputs are available. Refresh the Refinement before opening this drawer.
        </p>
      )}

      {activeDraft && (
        <div className="grid gap-4 lg:grid-cols-[220px_minmax(0,1fr)]">
          <nav
            aria-label="Evidence selected for classification"
            className="rounded-xl border border-gray-200 bg-gray-50/70 p-3 dark:border-gray-700 dark:bg-gray-900/30"
          >
            <p className="px-1 text-[10px] font-semibold uppercase tracking-wide text-gray-400">
              {drafts.length} selected
            </p>
            <div className="mt-2 space-y-2">
              {drafts.map((draft, index) => {
                const evidence = evidenceById.get(draft.evidence_id);
                const itemLabel = evidence?.claim?.trim()
                  || evidence?.relative_path
                  || `Evidence ${index + 1}`;
                const selected = draft.evidence_id === activeDraft.evidence_id;
                const ready = Boolean(
                  draft.source_role
                  && draft.relevance_summary.trim()
                  && draft.scope_relation.trim()
                  && draft.source_origin.trim()
                  && (!needsInterpretationLimit(draft.source_role)
                    || draft.interpretation_limit.trim()),
                );
                return (
                  <button
                    key={draft.evidence_id}
                    type="button"
                    data-testid={`legacy-classification-item-${index + 1}`}
                    aria-current={selected ? 'step' : undefined}
                    onClick={() => onActiveEvidenceChange(draft.evidence_id)}
                    className={`w-full rounded-lg border p-3 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500 ${selected
                      ? 'border-cyan-500 bg-cyan-50/70 dark:border-cyan-500 dark:bg-cyan-950/30'
                      : 'border-gray-200 bg-white hover:border-gray-300 dark:border-gray-700 dark:bg-gray-900 dark:hover:border-gray-600'}`}
                  >
                    <span className="block truncate text-xs font-semibold text-gray-800 dark:text-gray-100" title={itemLabel}>
                      {itemLabel}
                    </span>
                    <span className={`mt-1 block text-[11px] ${ready
                      ? 'text-emerald-700 dark:text-emerald-300'
                      : selected
                        ? 'text-amber-700 dark:text-amber-300'
                        : 'text-gray-400'}`}
                    >
                      {ready ? 'Classification ready' : selected ? 'Classification required' : 'Not reviewed'}
                    </span>
                  </button>
                );
              })}
            </div>
          </nav>

          <div className="space-y-4">
            {([activeDraft] as const).map((draft) => {
        const index = activeIndex;
        const evidence = evidenceById.get(draft.evidence_id);
        const itemLabel = evidence?.claim?.trim()
          || evidence?.relative_path
          || `Evidence ${index + 1}`;
        const visibleIssues = showIssues ? issues : [];
        return (
          <fieldset
            key={draft.evidence_id}
            className="space-y-3 rounded-xl border border-gray-200 p-4 dark:border-gray-700"
            data-legacy-step-focus="classify"
            tabIndex={-1}
          >
            <legend className="sr-only">Classification for {itemLabel}</legend>
            <section className="rounded-lg border border-gray-200 bg-gray-50/70 p-3 dark:border-gray-700 dark:bg-gray-900/30">
              <p className="text-[10px] font-semibold uppercase tracking-wide text-gray-400">
                Observed claim
              </p>
              <p className="mt-1.5 text-xs font-semibold leading-5 text-gray-900 dark:text-white">
                {itemLabel}
              </p>
              <p className="mt-1 text-[11px] text-gray-500 dark:text-gray-400">
                <span className="text-gray-400">Origin:</span> Source investigation
              </p>
            </section>

            <fieldset
              aria-label={`Source role for evidence ${index + 1}`}
              className="space-y-2"
            >
              <legend className="text-xs font-semibold text-gray-700 dark:text-gray-200">
                What does this observation represent?
              </legend>
              {!draft.source_role && (
                <p className="text-[11px] leading-4 text-gray-500 dark:text-gray-400">
                  No option is selected automatically.
                </p>
              )}
              <div className="grid gap-2 sm:grid-cols-2">
                {AUTHORED_ROLES.map((role) => {
                  const selected = draft.source_role === role;
                  return (
                    <label
                      key={role}
                      className={`flex cursor-pointer items-start gap-2 rounded-lg border px-3 py-2.5 text-left transition-colors ${selected
                        ? 'border-cyan-500 bg-cyan-50/70 dark:border-cyan-500 dark:bg-cyan-950/30'
                        : 'border-gray-200 bg-white hover:border-gray-300 dark:border-gray-700 dark:bg-gray-900 dark:hover:border-gray-600'}`}
                    >
                      <input
                        type="radio"
                        name={`source-role-${draft.evidence_id}`}
                        value={role}
                        checked={selected}
                        data-evidence-id={draft.evidence_id}
                        data-validation-field="source_role"
                        onChange={() => updateDraft(index, {
                          source_role: role,
                          ...(!needsInterpretationLimit(role)
                            ? { interpretation_limit: '' }
                            : {}),
                        })}
                        className="mt-0.5 h-4 w-4 shrink-0 accent-cyan-600"
                      />
                      <span className="min-w-0">
                        <span className="block text-xs font-semibold text-gray-800 dark:text-gray-100">
                          {codeEvidenceSourceRoleLabel(role)}
                        </span>
                        <span className="mt-0.5 block text-[11px] font-normal leading-4 text-gray-500 dark:text-gray-400">
                          {SOURCE_ROLE_DESCRIPTIONS[role]}
                        </span>
                      </span>
                    </label>
                  );
                })}
              </div>
              <FieldIssue issues={visibleIssues} evidenceId={draft.evidence_id} field="source_role" />
            </fieldset>

            <label className="block text-xs font-semibold text-gray-700 dark:text-gray-200">
              Why is it relevant?
              <textarea
                aria-label={`Relevance summary for evidence ${index + 1}`}
                value={draft.relevance_summary}
                data-evidence-id={draft.evidence_id}
                data-validation-field="relevance_summary"
                rows={2}
                onChange={(event) => updateDraft(index, { relevance_summary: event.target.value })}
                placeholder="Describe the delivery relevance in plain language."
                className="mt-1 w-full resize-y rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm dark:border-gray-700 dark:bg-gray-900"
              />
              <FieldIssue issues={visibleIssues} evidenceId={draft.evidence_id} field="relevance_summary" />
            </label>

            <div className="grid gap-3 sm:grid-cols-2">
              <label className="block text-xs font-semibold text-gray-700 dark:text-gray-200">
                Relationship to this scope
                <textarea
                  aria-label={`Scope relation for evidence ${index + 1}`}
                  value={draft.scope_relation}
                  data-evidence-id={draft.evidence_id}
                  data-validation-field="scope_relation"
                  rows={2}
                  onChange={(event) => updateDraft(index, { scope_relation: event.target.value })}
                  placeholder="For example, same capability"
                  className="mt-1 w-full resize-y rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm dark:border-gray-700 dark:bg-gray-900"
                />
                <FieldIssue issues={visibleIssues} evidenceId={draft.evidence_id} field="scope_relation" />
              </label>
              <label className="block text-xs font-semibold text-gray-700 dark:text-gray-200">
                Source origin
                <textarea
                  aria-label={`Source origin for evidence ${index + 1}`}
                  value={draft.source_origin}
                  data-evidence-id={draft.evidence_id}
                  data-validation-field="source_origin"
                  rows={2}
                  onChange={(event) => updateDraft(index, { source_origin: event.target.value })}
                  placeholder="For example, product codebase"
                  className="mt-1 w-full resize-y rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm dark:border-gray-700 dark:bg-gray-900"
                />
                <FieldIssue issues={visibleIssues} evidenceId={draft.evidence_id} field="source_origin" />
              </label>
            </div>

            {needsInterpretationLimit(draft.source_role) && (
              <label className="block text-xs font-semibold text-gray-700 dark:text-gray-200">
                What this does not prove <span className="font-normal text-gray-400">(required for scaffold or reference)</span>
                <textarea
                  aria-label={`Interpretation limit for evidence ${index + 1}`}
                  value={draft.interpretation_limit}
                  data-evidence-id={draft.evidence_id}
                  data-validation-field="interpretation_limit"
                  rows={2}
                  onChange={(event) => updateDraft(index, { interpretation_limit: event.target.value })}
                  placeholder="State the interpretation limit."
                  className="mt-1 w-full resize-y rounded-lg border border-amber-300 bg-amber-50/40 px-3 py-2 text-sm dark:border-amber-800 dark:bg-amber-950/20"
                />
                <FieldIssue issues={visibleIssues} evidenceId={draft.evidence_id} field="interpretation_limit" />
              </label>
            )}

            <CanonicalBaseline draft={draft} issues={visibleIssues} />

            {draft.provenance_note_required && (
              <label className="block text-xs font-semibold text-gray-700 dark:text-gray-200">
                What was already present before delivery work?
                <span className="mt-0.5 block text-[11px] font-normal leading-4 text-gray-500 dark:text-gray-400">
                  Required only when the baseline is a pre-existing worktree.
                </span>
                <textarea
                  aria-label={`Baseline provenance note for evidence ${index + 1}`}
                  value={draft.provenance_note}
                  data-evidence-id={draft.evidence_id}
                  data-validation-field="provenance_note"
                  rows={2}
                  onChange={(event) => updateDraft(index, { provenance_note: event.target.value })}
                  placeholder="Describe the pre-existing source state in plain language."
                  className="mt-1 w-full resize-y rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm dark:border-gray-700 dark:bg-gray-900"
                />
                <FieldIssue issues={visibleIssues} evidenceId={draft.evidence_id} field="provenance_note" />
              </label>
            )}

            <TechnicalDetails
              evidenceId={draft.evidence_id}
              workspaceStateId={draft.baseline_workspace_state_id}
              payloadSha256={draft.expected_evidence_payload_sha256}
              classificationRevision={draft.expected_classification_revision}
              issues={visibleIssues}
            />
          </fieldset>
        );
            })}

            <label className="block text-xs font-semibold text-gray-700 dark:text-gray-200">
              Classification justification
              <textarea
                aria-label="Classification justification"
                value={justification}
                data-validation-field="justification"
                rows={3}
                onChange={(event) => onJustificationChange(event.target.value)}
                placeholder="Explain the classification decision for the audit trail."
                className="mt-1 w-full resize-y rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm dark:border-gray-700 dark:bg-gray-900"
              />
              <FieldIssue
                issues={showIssues ? issues : []}
                evidenceId={null}
                field="justification"
              />
            </label>
          </div>
        </div>
      )}
    </div>
  );
}

function ReviewStep({
  reviewed,
  snapshot,
}: {
  reviewed: ReviewedLegacyClassificationBatch;
  snapshot: LegacyEvidenceClassificationSnapshot;
}) {
  const evidenceById = new Map(snapshot.evidence.map((item) => [item.id, item]));
  const inputById = new Map(
    snapshot.classificationInputs.map((input) => [input.evidence_id, input]),
  );
  return (
    <div
      className="space-y-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500"
      data-testid="legacy-classification-review-step"
      data-legacy-step-focus="review"
      tabIndex={-1}
    >
      <p className="flex items-start gap-2 rounded-lg border border-blue-200 bg-blue-50/70 p-3 text-xs leading-5 text-blue-800 dark:border-blue-800 dark:bg-blue-950/30 dark:text-blue-200" role="note">
        <ShieldCheck size={15} className="mt-0.5 shrink-0" aria-hidden="true" />
        This adds an audited classification. The original Evidence remains unchanged. Review is read-only, and Apply sends every item below as one atomic batch.
      </p>

      {reviewed.request.items.map((item, index) => {
        const evidence = evidenceById.get(item.evidence_id);
        const input = inputById.get(item.evidence_id);
        return (
          <section key={item.evidence_id} className="rounded-xl border border-gray-200 p-4 dark:border-gray-700">
            <h4 className="text-sm font-semibold text-gray-900 dark:text-white">
              {evidence?.claim?.trim() || evidence?.relative_path || `Evidence ${index + 1}`}
            </h4>
            <p className="mt-0.5 text-[11px] text-gray-500 dark:text-gray-400">
              Short origin: {shortOrigin(evidence)}
            </p>
            <dl className="mt-3 grid gap-x-4 gap-y-3 text-xs sm:grid-cols-2">
              <div>
                <dt className="font-semibold text-gray-500 dark:text-gray-400">Source role</dt>
                <dd className="mt-0.5 text-gray-800 dark:text-gray-100">
                  {codeEvidenceSourceRoleLabel(item.source_role)}
                </dd>
              </div>
              <div>
                <dt className="font-semibold text-gray-500 dark:text-gray-400">Baseline</dt>
                <dd className="mt-0.5 text-gray-800 dark:text-gray-100">
                  {codeEvidenceBaselinePresenceLabel(item.baseline_provenance.presence)}
                </dd>
              </div>
              <div className="sm:col-span-2">
                <dt className="font-semibold text-gray-500 dark:text-gray-400">Relevance</dt>
                <dd className="mt-0.5 leading-5 text-gray-800 dark:text-gray-100">{item.relevance_summary}</dd>
              </div>
              <div>
                <dt className="font-semibold text-gray-500 dark:text-gray-400">Relation to this delivery</dt>
                <dd className="mt-0.5 leading-5 text-gray-800 dark:text-gray-100">{item.scope_relation}</dd>
              </div>
              <div>
                <dt className="font-semibold text-gray-500 dark:text-gray-400">Source origin</dt>
                <dd className="mt-0.5 leading-5 text-gray-800 dark:text-gray-100">{item.source_origin}</dd>
              </div>
              {needsInterpretationLimit(item.source_role) && item.interpretation_limit && (
                <div className="sm:col-span-2">
                  <dt className="font-semibold text-gray-500 dark:text-gray-400">Interpretation limit</dt>
                  <dd className="mt-0.5 leading-5 text-gray-800 dark:text-gray-100">{item.interpretation_limit}</dd>
                </div>
              )}
              {input?.baseline_provenance.provenance_note_required
                && item.baseline_provenance.provenance_note && (
                <div className="sm:col-span-2">
                  <dt className="font-semibold text-gray-500 dark:text-gray-400">Baseline provenance note</dt>
                  <dd className="mt-0.5 leading-5 text-gray-800 dark:text-gray-100">
                    {item.baseline_provenance.provenance_note}
                  </dd>
                </div>
              )}
            </dl>
            <div className="mt-3">
              <TechnicalDetails
                evidenceId={item.evidence_id}
                workspaceStateId={item.baseline_provenance.workspace_state_id}
                payloadSha256={item.expected_evidence_payload_sha256}
                classificationRevision={item.expected_classification_revision}
              />
            </div>
          </section>
        );
      })}

      <section className="rounded-lg border border-gray-200 bg-gray-50/70 p-3 text-xs dark:border-gray-700 dark:bg-gray-900/30">
        <h4 className="font-semibold text-gray-800 dark:text-gray-100">Batch justification</h4>
        <p className="mt-1 leading-5 text-gray-700 dark:text-gray-200">
          {reviewed.request.justification}
        </p>
      </section>
    </div>
  );
}

export function LegacyEvidenceClassificationDrawer({
  snapshot: initialSnapshot,
  canClassify,
  opener,
  focusFallback,
  onClose,
  onApplyBatch,
  onCanonicalRefetch,
  onApplied,
  createIdempotencyKey = defaultIdempotencyKey,
}: LegacyEvidenceClassificationDrawerProps) {
  const titleId = useId();
  const descriptionId = useId();
  const restoreFocusFallback = useCallback(
    () => (opener?.isConnected ? opener : focusFallback?.isConnected ? focusFallback : null),
    [focusFallback, opener],
  );
  const focusTrap = useDialogFocusTrap(
    canClassify,
    '[data-legacy-classification-initial-focus]',
    restoreFocusFallback,
  );
  const intentStoreRef = useRef<LegacyClassificationIntentStore | null>(null);
  if (!intentStoreRef.current) {
    intentStoreRef.current = createLegacyClassificationIntentStore(createIdempotencyKey);
  }
  const intentStore = intentStoreRef.current;
  const [snapshot, setSnapshot] = useState(initialSnapshot);
  const [drafts, setDrafts] = useState(() => draftsFromSnapshot(initialSnapshot));
  const [activeEvidenceId, setActiveEvidenceId] = useState<string | null>(
    initialSnapshot.classificationInputs[0]?.evidence_id ?? null,
  );
  const [justification, setJustification] = useState('');
  const [step, setStep] = useState<DrawerStep>('classify');
  const [reviewed, setReviewed] = useState<ReviewedLegacyClassificationBatch | null>(null);
  const [showIssues, setShowIssues] = useState(false);
  const [serverIssues, setServerIssues] = useState<LegacyEvidenceClassificationDraftIssue[]>([]);
  const [pendingValidationFocus, setPendingValidationFocus] = useState<ValidationFocusTarget | null>(null);
  const [pendingStepFocus, setPendingStepFocus] = useState<DrawerStep | null>(null);
  const [failure, setFailure] = useState<SubmissionFailure | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const controllerRef = useRef<AbortController | null>(null);
  const operationRef = useRef(0);
  const submissionStartedRef = useRef(false);
  const conflictRefreshStartedRef = useRef(false);
  const busy = submitting || refreshing;

  useMaskOpenerDialog(canClassify, opener);
  useLockDocumentScroll(canClassify);

  const clearLocalState = useCallback(() => {
    operationRef.current += 1;
    controllerRef.current?.abort();
    controllerRef.current = null;
    intentStore.clear();
    setDrafts([]);
    setActiveEvidenceId(null);
    setJustification('');
    setReviewed(null);
    setFailure(null);
    setNotice(null);
    setServerIssues([]);
    setPendingValidationFocus(null);
    setPendingStepFocus(null);
    setShowIssues(false);
    setStep('classify');
    setSubmitting(false);
    setRefreshing(false);
    submissionStartedRef.current = false;
    conflictRefreshStartedRef.current = false;
  }, [intentStore]);

  const closeAndClear = useCallback(() => {
    clearLocalState();
    onClose();
  }, [clearLocalState, onClose]);

  useEscapeToClose(closeAndClear, {
    enabled: canClassify,
    canClose: !busy,
    priority: 170,
  });

  useEffect(() => {
    if (!canClassify) closeAndClear();
  }, [canClassify, closeAndClear]);

  useEffect(() => () => {
    operationRef.current += 1;
    controllerRef.current?.abort();
    intentStore.clear();
  }, [intentStore]);

  const issues = useMemo(
    () => [
      ...validateLegacyEvidenceClassificationDrafts(drafts, justification),
      ...serverIssues,
    ],
    [drafts, justification, serverIssues],
  );

  useEffect(() => {
    if (drafts.length === 0) {
      if (activeEvidenceId !== null) setActiveEvidenceId(null);
      return;
    }
    if (!drafts.some((draft) => draft.evidence_id === activeEvidenceId)) {
      setActiveEvidenceId(drafts[0].evidence_id);
    }
  }, [activeEvidenceId, drafts]);

  useEffect(() => {
    if (!pendingValidationFocus || step !== 'classify') return undefined;
    const focusRequestedTarget = () => {
      const requestedField = pendingValidationFocus.field === 'items'
        ? 'batch'
        : pendingValidationFocus.field;
      const candidates = focusTrap.dialogRef.current?.querySelectorAll<HTMLElement>(
        '[data-validation-field]',
      ) ?? [];
      const target = [...candidates].find((candidate) => (
        candidate.dataset.validationField === requestedField
        && (
          pendingValidationFocus.evidenceId === null
          || candidate.dataset.evidenceId === pendingValidationFocus.evidenceId
        )
      ));
      const collapsedDetails = target?.closest('details');
      if (collapsedDetails) collapsedDetails.open = true;
      target?.focus();
      setPendingValidationFocus(null);
    };
    const usesAnimationFrame = typeof requestAnimationFrame === 'function';
    const scheduled = usesAnimationFrame
      ? requestAnimationFrame(focusRequestedTarget)
      : window.setTimeout(focusRequestedTarget, 0);
    return () => {
      if (usesAnimationFrame && typeof cancelAnimationFrame === 'function') {
        cancelAnimationFrame(scheduled);
      } else {
        window.clearTimeout(scheduled);
      }
    };
  }, [focusTrap.dialogRef, pendingValidationFocus, step]);

  useEffect(() => {
    if (!pendingStepFocus || pendingStepFocus !== step) return undefined;
    const focusStep = () => {
      const target = focusTrap.dialogRef.current?.querySelector<HTMLElement>(
        `[data-legacy-step-focus="${pendingStepFocus}"]`,
      );
      (target ?? focusTrap.dialogRef.current)?.focus();
      setPendingStepFocus(null);
    };
    const usesAnimationFrame = typeof requestAnimationFrame === 'function';
    const scheduled = usesAnimationFrame
      ? requestAnimationFrame(focusStep)
      : window.setTimeout(focusStep, 0);
    return () => {
      if (usesAnimationFrame && typeof cancelAnimationFrame === 'function') {
        cancelAnimationFrame(scheduled);
      } else {
        window.clearTimeout(scheduled);
      }
    };
  }, [focusTrap.dialogRef, pendingStepFocus, step]);

  const clearServerValidation = () => {
    if (failure?.kind !== 'validation' && serverIssues.length === 0) return;
    setFailure(null);
    setServerIssues([]);
    setPendingValidationFocus(null);
  };

  const updateDrafts = (nextDrafts: LegacyEvidenceClassificationDraft[]) => {
    clearServerValidation();
    setDrafts(nextDrafts);
  };

  const updateJustification = (value: string) => {
    clearServerValidation();
    setJustification(value);
  };

  const moveToReview = () => {
    setShowIssues(true);
    const localIssues = validateLegacyEvidenceClassificationDrafts(drafts, justification);
    if (localIssues.length > 0) {
      const focusTarget = firstValidationFocus(localIssues);
      if (focusTarget.evidenceId) setActiveEvidenceId(focusTarget.evidenceId);
      setPendingValidationFocus(focusTarget);
      return;
    }
    try {
      const intent = buildLegacyEvidenceClassificationIntent(drafts, justification);
      const nextReview = intentStore.review(intent);
      setReviewed(nextReview);
      setFailure(null);
      setNotice(null);
      setServerIssues([]);
      setPendingValidationFocus(null);
      setPendingStepFocus('review');
      setStep('review');
    } catch {
      // Field-level issues are projected next to their owning controls.
    }
  };

  const moveBackToClassify = () => {
    if (busy || failure) return;
    intentStore.clearReview();
    setReviewed(null);
    setPendingStepFocus('classify');
    setStep('classify');
    setShowIssues(false);
  };

  const submitReviewed = async (batch: ReviewedLegacyClassificationBatch) => {
    if (
      submissionStartedRef.current
      || busy
      || !canClassify
      || (failure && failure.kind !== 'network_ambiguous')
    ) return;
    submissionStartedRef.current = true;
    const operationId = operationRef.current + 1;
    operationRef.current = operationId;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setSubmitting(true);
    setFailure(null);
    setNotice(null);

    let result: LegacyEvidenceClassificationBatchResult;
    try {
      result = await onApplyBatch(batch.request, controller.signal);
    } catch (caught) {
      if (controller.signal.aborted || operationId !== operationRef.current) return;
      if (caught instanceof LegacyEvidenceClassificationConflictError) {
        conflictRefreshStartedRef.current = false;
        if (caught.kind === 'idempotency') {
          setFailure({ kind: 'idempotency_conflict', message: caught.message });
        } else {
          setFailure({
            kind: 'canonical_conflict',
            message: caught.message,
            refreshUsed: false,
          });
        }
      } else if (caught instanceof AuthenticatedFetchError && caught.status === 403) {
        setFailure({ kind: 'forbidden', message: caught.message });
      } else if (caught instanceof AuthenticatedFetchError && caught.status === 404) {
        setFailure({ kind: 'not_found', message: caught.message });
      } else if (caught instanceof AuthenticatedFetchError && caught.status === 422) {
        const validationIssues = serverValidationIssues(caught, batch);
        intentStore.invalidateReview();
        setReviewed(null);
        setServerIssues(validationIssues);
        const validationFocus = firstValidationFocus(validationIssues);
        if (validationFocus.evidenceId) setActiveEvidenceId(validationFocus.evidenceId);
        setPendingValidationFocus(validationFocus);
        setShowIssues(true);
        setStep('classify');
        setFailure({ kind: 'validation', message: caught.message });
      } else if (
        caught instanceof AuthenticatedFetchError
        && caught.status > 0
        && caught.status < 500
        && !caught.retryable
      ) {
        setFailure({ kind: 'deterministic', message: caught.message });
      } else {
        setFailure({ kind: 'network_ambiguous', message: errorMessage(caught) });
      }
      submissionStartedRef.current = false;
      setSubmitting(false);
      return;
    }

    if (controller.signal.aborted || operationId !== operationRef.current) return;
    intentStore.clear();
    try {
      await onCanonicalRefetch(controller.signal);
      if (controller.signal.aborted || operationId !== operationRef.current) return;
      await onApplied?.(result);
      if (controller.signal.aborted || operationId !== operationRef.current) return;
      closeAndClear();
    } catch (caught) {
      if (controller.signal.aborted || operationId !== operationRef.current) return;
      setFailure({
        kind: 'applied_refresh_failed',
        message: errorMessage(caught),
      });
      submissionStartedRef.current = false;
      setSubmitting(false);
    }
  };

  const retryExactBatch = () => {
    if (failure?.kind !== 'network_ambiguous' || busy) return;
    void submitReviewed(intentStore.exactRetry());
  };

  const editAfterAmbiguousOutcome = () => {
    if (failure?.kind !== 'network_ambiguous' || busy) return;
    intentStore.invalidateReview();
    setReviewed(null);
    setFailure(null);
    setNotice('Edit the preserved draft, then review it as a new submission.');
    setShowIssues(false);
    setPendingStepFocus('classify');
    setStep('classify');
  };

  const reviewWithNewIdempotencyKey = () => {
    if (failure?.kind !== 'idempotency_conflict' || busy) return;
    intentStore.invalidateReview();
    setReviewed(null);
    setFailure(null);
    setNotice('Review the preserved draft again before applying as a new submission.');
    setShowIssues(false);
    setPendingStepFocus('classify');
    setStep('classify');
  };

  const refreshAfterConflict = async () => {
    if (
      failure?.kind !== 'canonical_conflict'
      || failure.refreshUsed
      || conflictRefreshStartedRef.current
      || busy
    ) return;
    conflictRefreshStartedRef.current = true;
    setFailure({ ...failure, refreshUsed: true });
    const operationId = operationRef.current + 1;
    operationRef.current = operationId;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setRefreshing(true);
    try {
      const refreshedSnapshot = await onCanonicalRefetch(controller.signal);
      if (controller.signal.aborted || operationId !== operationRef.current) return;
      setDrafts((current) => reconcileDrafts(current, refreshedSnapshot));
      setSnapshot(refreshedSnapshot);
      intentStore.invalidateReview();
      setReviewed(null);
      setFailure(null);
      setNotice('Canonical values refreshed. Review the preserved draft again before applying.');
      setShowIssues(false);
      setPendingStepFocus('classify');
      setStep('classify');
    } catch (caught) {
      if (controller.signal.aborted || operationId !== operationRef.current) return;
      setFailure({
        kind: 'canonical_conflict',
        refreshUsed: true,
        message: `Canonical refresh failed: ${errorMessage(caught)}`,
      });
    } finally {
      if (operationId === operationRef.current) setRefreshing(false);
    }
  };

  if (!canClassify) return null;

  const drawer = (
    <div
      className="fixed inset-0 z-[170] flex bg-black/60"
      data-testid="legacy-evidence-classification-overlay"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) closeAndClear();
      }}
    >
      <div
        ref={focusTrap.dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        aria-busy={busy}
        tabIndex={-1}
        onKeyDown={focusTrap.onKeyDown}
        className="ml-auto flex h-full w-full max-w-2xl flex-col overflow-hidden border-l border-gray-200 bg-white shadow-2xl dark:border-gray-700 dark:bg-gray-900"
        data-testid="legacy-evidence-classification-drawer"
      >
        <header className="border-b border-gray-200 px-4 py-4 dark:border-gray-700 sm:px-6">
          <div className="flex items-start justify-between gap-4">
            <div className="min-w-0">
              <h2 id={titleId} className="text-base font-semibold text-gray-900 dark:text-white">
                Classify legacy Evidence
              </h2>
              <p id={descriptionId} className="mt-1 text-xs leading-5 text-gray-500 dark:text-gray-400">
                Add explicit delivery meaning to historical observations. Nothing in the original
                Evidence will be edited.
              </p>
            </div>
            <button
              type="button"
              data-legacy-classification-initial-focus
              disabled={busy}
              onClick={closeAndClear}
              aria-label="Close legacy evidence classification"
              className="rounded-lg p-1.5 text-gray-400 hover:bg-gray-100 hover:text-gray-700 disabled:opacity-40 dark:hover:bg-gray-800 dark:hover:text-gray-200"
            >
              <X size={18} aria-hidden="true" />
            </button>
          </div>

          <ol className="mt-4 flex items-center gap-2 text-xs" aria-label="Classification steps">
            <li
              aria-current={step === 'classify' ? 'step' : undefined}
              className={`flex items-center gap-1.5 font-semibold ${step === 'classify' ? 'text-cyan-700 dark:text-cyan-300' : 'text-emerald-700 dark:text-emerald-300'}`}
            >
              <span className="flex h-5 w-5 items-center justify-center rounded-full border border-current">
                {step === 'review' ? <Check size={12} aria-hidden="true" /> : '1'}
              </span>
              Classify
            </li>
            <ChevronRight size={13} className="text-gray-300" aria-hidden="true" />
            <li
              aria-current={step === 'review' ? 'step' : undefined}
              className={`flex items-center gap-1.5 font-semibold ${step === 'review' ? 'text-cyan-700 dark:text-cyan-300' : 'text-gray-400'}`}
            >
              <span className="flex h-5 w-5 items-center justify-center rounded-full border border-current">2</span>
              Review
            </li>
          </ol>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4 sm:px-6">
          {notice && (
            <p className="mb-4 rounded-lg border border-blue-200 bg-blue-50 p-3 text-xs leading-5 text-blue-800 dark:border-blue-800 dark:bg-blue-950/30 dark:text-blue-200" role="status" aria-live="polite">
              {notice}
            </p>
          )}

          {step === 'classify' ? (
            <ClassifyStep
              snapshot={snapshot}
              drafts={drafts}
              justification={justification}
              issues={issues}
              showIssues={showIssues}
              activeEvidenceId={activeEvidenceId}
              onDraftsChange={updateDrafts}
              onJustificationChange={updateJustification}
              onActiveEvidenceChange={setActiveEvidenceId}
            />
          ) : reviewed ? (
            <ReviewStep reviewed={reviewed} snapshot={snapshot} />
          ) : null}

          {failure && (
            <section className="mt-4 rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-800 dark:border-red-800 dark:bg-red-950/30 dark:text-red-200" role="alert" aria-live="assertive">
              <div className="flex items-start gap-2">
                <AlertTriangle size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
                <div>
                  <p className="font-semibold">{submissionFailureTitle(failure)}</p>
                  <p className="mt-1 leading-5">{failure.message}</p>
                  {failure.kind === 'network_ambiguous' && (
                    <p className="mt-1 leading-5">
                      Retry sends the exact same reviewed bytes and idempotency key without refreshing first.
                    </p>
                  )}
                  {failure.kind === 'canonical_conflict' && !failure.refreshUsed && (
                    <p className="mt-1 leading-5">
                      Refresh once, then review the preserved draft again with a new idempotency key.
                    </p>
                  )}
                  {failure.kind === 'idempotency_conflict' && (
                    <p className="mt-1 leading-5">
                      Preserve the draft and review it as a new submission before applying again.
                    </p>
                  )}
                  {failure.kind === 'forbidden' && (
                    <p className="mt-1 leading-5">
                      No changes were applied. Close the drawer and ask a board administrator to restore access.
                    </p>
                  )}
                  {failure.kind === 'not_found' && (
                    <p className="mt-1 leading-5">
                      No changes were applied. Close the drawer and reload the Refinement.
                    </p>
                  )}
                  {failure.kind === 'validation' && (
                    <p className="mt-1 leading-5">
                      The batch was not applied. Correct the highlighted value, then review the whole batch again.
                    </p>
                  )}
                  {failure.kind === 'applied_refresh_failed' && (
                    <p className="mt-1 leading-5">
                      Do not apply this batch again. Close the drawer and reload the Refinement.
                    </p>
                  )}
                </div>
              </div>
            </section>
          )}
        </div>

        {busy && (
          <p className="sr-only" role="status" aria-live="polite">
            {submitting ? 'Applying classification.' : 'Refreshing canonical context.'}
          </p>
        )}

        <footer className="flex flex-col gap-3 border-t border-gray-200 px-4 py-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] dark:border-gray-700 sm:flex-row sm:items-center sm:justify-between sm:px-6">
          <div className="w-full sm:w-auto">
            {step === 'review' && !failure && (
              <button
                type="button"
                disabled={busy}
                onClick={moveBackToClassify}
                className="btn btn-secondary inline-flex w-full items-center justify-center gap-1.5 text-xs sm:w-auto"
              >
                <ArrowLeft size={13} aria-hidden="true" /> Back to classify
              </button>
            )}
            {step === 'classify' && (
              <p className="text-[11px] leading-4 text-gray-500 dark:text-gray-400">
                Your changes are not saved until you review and apply the complete batch.
              </p>
            )}
          </div>
          <div className="flex w-full flex-col gap-2 sm:w-auto sm:flex-row sm:flex-wrap sm:justify-end">
            <button
              type="button"
              disabled={busy}
              onClick={closeAndClear}
              className="btn btn-secondary w-full text-xs sm:w-auto"
            >
              Cancel
            </button>
            {step === 'classify' && (
              <button
                type="button"
                disabled={busy || drafts.length === 0}
                onClick={moveToReview}
                className="btn btn-primary w-full text-xs disabled:cursor-not-allowed disabled:opacity-50 sm:w-auto"
              >
                Review batch
              </button>
            )}
            {step === 'review' && reviewed && !failure && (
              <button
                type="button"
                disabled={busy}
                onClick={() => void submitReviewed(reviewed)}
                className="btn btn-primary w-full text-xs disabled:cursor-not-allowed disabled:opacity-50 sm:w-auto"
              >
                {submitting ? 'Applying…' : 'Apply classification'}
              </button>
            )}
            {failure?.kind === 'network_ambiguous' && (
              <button
                type="button"
                disabled={busy}
                onClick={editAfterAmbiguousOutcome}
                className="btn btn-secondary w-full text-xs disabled:cursor-not-allowed disabled:opacity-50 sm:w-auto"
              >
                Edit and review again
              </button>
            )}
            {failure?.kind === 'network_ambiguous' && (
              <button
                type="button"
                disabled={busy}
                onClick={retryExactBatch}
                className="btn btn-primary w-full text-xs disabled:cursor-not-allowed disabled:opacity-50 sm:w-auto"
              >
                {submitting ? 'Retrying…' : 'Retry exact batch'}
              </button>
            )}
            {failure?.kind === 'canonical_conflict' && (
              <button
                type="button"
                disabled={busy || failure.refreshUsed}
                onClick={() => void refreshAfterConflict()}
                className="btn btn-primary inline-flex w-full items-center justify-center gap-1.5 text-xs disabled:cursor-not-allowed disabled:opacity-50 sm:w-auto"
              >
                <RefreshCw size={13} className={refreshing ? 'animate-spin' : ''} aria-hidden="true" />
                {refreshing ? 'Refreshing…' : 'Refresh canonical context'}
              </button>
            )}
            {failure?.kind === 'idempotency_conflict' && (
              <button
                type="button"
                disabled={busy}
                onClick={reviewWithNewIdempotencyKey}
                className="btn btn-primary w-full text-xs disabled:cursor-not-allowed disabled:opacity-50 sm:w-auto"
              >
                Review as new submission
              </button>
            )}
          </div>
        </footer>
      </div>
    </div>
  );

  return typeof document === 'undefined'
    ? drawer
    : createPortal(drawer, document.body);
}
