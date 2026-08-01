import {
  type ReactNode,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from 'react';
import { createPortal } from 'react-dom';
import {
  AlertTriangle,
  Ban,
  CheckCircle2,
  FileWarning,
  Plus,
  RefreshCw,
  ShieldCheck,
  Trash2,
  X,
  XCircle,
} from 'lucide-react';

import { useDialogFocusTrap } from '@/hooks/useDialogFocusTrap';
import { useEscapeToClose } from '@/hooks/useEscapeToClose';
import { usePermissions } from '@/hooks/usePermissions';
import {
  PolicyGovernanceApiError,
  usePolicyGovernanceApi,
} from '@/services/policy-governance-api';
import type {
  SemanticEvidenceRef,
  SemanticFindingDetail,
  SemanticWaiverFull,
} from '@/types/policy-governance';

import {
  createPolicyUiId,
  formatPolicyTimestamp,
  formatPolicyToken,
} from './policyUiModel';
import {
  emptySemanticEvidenceDraft,
  parseRequestedSemanticWaiverResponse,
  parseReviewedSemanticWaiverResponse,
  parseRevokedSemanticWaiverResponse,
  parseRevalidatedSemanticWaiverResponse,
  parseSemanticEvidenceDrafts,
  parseSemanticWaiverHeadResponse,
  policyWaiverErrorMessage,
  type SemanticEvidenceDraft,
  type SemanticWaiverMutationResult,
} from './policyWaiverModel';

export type PolicyWaiverMutationResult = SemanticWaiverMutationResult;

export type PolicyWaiverAction =
  | 'approve'
  | 'reject'
  | 'revoke'
  | 'revalidate';

interface DialogIdentity {
  signature: string;
  idempotencyKey: string;
}

function newDialogIdentity(prefix: string): DialogIdentity {
  return {
    signature: '',
    idempotencyKey: createPolicyUiId(`${prefix}-command`),
  };
}

function useMaskOpenerDialog() {
  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    const parent = opener?.closest<HTMLElement>(
      '[role="dialog"], [role="alertdialog"]',
    );
    if (!parent) return undefined;
    const hidden = parent.getAttribute('aria-hidden');
    const modal = parent.getAttribute('aria-modal');
    parent.setAttribute('aria-hidden', 'true');
    parent.removeAttribute('aria-modal');
    return () => {
      if (hidden === null) parent.removeAttribute('aria-hidden');
      else parent.setAttribute('aria-hidden', hidden);
      if (modal === null) parent.removeAttribute('aria-modal');
      else parent.setAttribute('aria-modal', modal);
    };
  }, []);
}

function toIsoTimestamp(value: string): string | null {
  if (!value.trim()) return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString();
}

function draftsFromEvidence(
  evidence: readonly SemanticEvidenceRef[],
): SemanticEvidenceDraft[] {
  return evidence.length > 0
    ? evidence.map((reference) => ({
        sourceType: reference.source_type,
        sourceId: reference.source_id,
        sourceVersion: String(reference.source_version),
        contentHash: reference.content_hash,
      }))
    : [emptySemanticEvidenceDraft()];
}

function WaiverScope({
  finding,
  waiver,
}: {
  finding?: SemanticFindingDetail;
  waiver?: SemanticWaiverFull;
}) {
  const source = finding ?? waiver;
  if (!source) return null;
  const metricResultId = finding?.metric_result_id
    ?? waiver?.metric_result_id;
  return (
    <section
      className="rounded-lg border border-surface-200 bg-surface-50 p-3 text-xs dark:border-surface-700 dark:bg-surface-950/40"
      data-testid="policy-waiver-exact-scope"
    >
      <h4 className="font-semibold text-surface-800 dark:text-surface-100">
        Server-owned semantic metric scope
      </h4>
      <p className="mt-1 text-surface-600 dark:text-surface-300">
        These identities are read-only. The server anchors the exception to
        this exact metric result and finding.
      </p>
      <dl className="mt-2 grid gap-x-4 gap-y-1 sm:grid-cols-2">
        <div>
          <dt className="inline font-semibold">Subject: </dt>
          <dd className="inline break-all">
            {formatPolicyToken(source.entity_type)}
            {' · '}
            {source.subject_id}
            {' · v'}
            {source.subject_version}
          </dd>
        </div>
        <div>
          <dt className="inline font-semibold">Metric: </dt>
          <dd className="inline break-all">{source.metric_code}</dd>
        </div>
        <div>
          <dt className="inline font-semibold">Metric result: </dt>
          <dd className="inline break-all">{metricResultId}</dd>
        </div>
        <div>
          <dt className="inline font-semibold">Finding: </dt>
          <dd className="inline break-all">{source.finding_id}</dd>
        </div>
        <div>
          <dt className="inline font-semibold">Receipt: </dt>
          <dd className="inline break-all">{source.receipt_id}</dd>
        </div>
        <div>
          <dt className="inline font-semibold">Guideline revision: </dt>
          <dd className="inline break-all">
            {source.guideline_revision_id}
          </dd>
        </div>
      </dl>
    </section>
  );
}

function EvidenceEditor({
  drafts,
  busy,
  onChange,
}: {
  drafts: SemanticEvidenceDraft[];
  busy: boolean;
  onChange: (drafts: SemanticEvidenceDraft[]) => void;
}) {
  const update = (
    index: number,
    field: keyof SemanticEvidenceDraft,
    value: string,
  ) => {
    onChange(drafts.map((draft, candidate) =>
      candidate === index ? { ...draft, [field]: value } : draft,
    ));
  };
  return (
    <fieldset
      disabled={busy}
      className="space-y-2 rounded-lg border border-surface-200 p-3 dark:border-surface-700"
    >
      <div className="flex items-center justify-between gap-2">
        <div>
          <legend className="text-xs font-semibold text-surface-700 dark:text-surface-200">
            Structured evidence
          </legend>
          <p className="mt-0.5 text-[11px] text-surface-500">
            Every reference identifies one immutable source version and its
            SHA-256 content hash.
          </p>
        </div>
        <button
          type="button"
          onClick={() =>
            onChange([...drafts, emptySemanticEvidenceDraft()])}
          className="inline-flex min-h-8 items-center gap-1 rounded-lg border border-surface-300 px-2 text-xs font-semibold text-surface-700 dark:border-surface-600 dark:text-surface-200"
        >
          <Plus size={13} aria-hidden="true" />
          Add reference
        </button>
      </div>
      {drafts.map((draft, index) => (
        <div
          key={index}
          className="grid gap-2 rounded-lg bg-surface-50 p-2 dark:bg-surface-950/40 sm:grid-cols-[1fr_1.4fr_90px_1.8fr_auto]"
          data-testid={`semantic-evidence-row-${index}`}
        >
          <label className="text-[11px] font-semibold text-surface-600 dark:text-surface-300">
            Source type
            <input
              value={draft.sourceType}
              onChange={(event) =>
                update(index, 'sourceType', event.target.value)}
              placeholder="artifact"
              className="mt-1 w-full rounded-md border border-surface-300 bg-white px-2 py-1.5 text-xs dark:border-surface-700 dark:bg-surface-900"
            />
          </label>
          <label className="text-[11px] font-semibold text-surface-600 dark:text-surface-300">
            Source ID
            <input
              value={draft.sourceId}
              onChange={(event) =>
                update(index, 'sourceId', event.target.value)}
              placeholder="spec-123"
              className="mt-1 w-full rounded-md border border-surface-300 bg-white px-2 py-1.5 text-xs dark:border-surface-700 dark:bg-surface-900"
            />
          </label>
          <label className="text-[11px] font-semibold text-surface-600 dark:text-surface-300">
            Version
            <input
              type="number"
              min={1}
              step={1}
              value={draft.sourceVersion}
              onChange={(event) =>
                update(index, 'sourceVersion', event.target.value)}
              className="mt-1 w-full rounded-md border border-surface-300 bg-white px-2 py-1.5 text-xs dark:border-surface-700 dark:bg-surface-900"
            />
          </label>
          <label className="text-[11px] font-semibold text-surface-600 dark:text-surface-300">
            SHA-256 content hash
            <input
              value={draft.contentHash}
              onChange={(event) =>
                update(index, 'contentHash', event.target.value)}
              placeholder="64 lowercase hexadecimal characters"
              className="mt-1 w-full rounded-md border border-surface-300 bg-white px-2 py-1.5 font-mono text-[10px] dark:border-surface-700 dark:bg-surface-900"
            />
          </label>
          <button
            type="button"
            aria-label={`Remove evidence reference ${index + 1}`}
            disabled={drafts.length === 1}
            onClick={() =>
              onChange(drafts.filter((_, candidate) => candidate !== index))}
            className="mt-5 self-start rounded-md p-1.5 text-surface-400 hover:bg-red-50 hover:text-red-600 disabled:opacity-30 dark:hover:bg-red-950/40"
          >
            <Trash2 size={14} aria-hidden="true" />
          </button>
        </div>
      ))}
    </fieldset>
  );
}

function DialogFrame({
  title,
  description,
  icon,
  busy,
  valid,
  submitLabel,
  submittingLabel,
  error,
  onClose,
  onSubmit,
  children,
}: {
  title: string;
  description: string;
  icon: ReactNode;
  busy: boolean;
  valid: boolean;
  submitLabel: string;
  submittingLabel: string;
  error: string | null;
  onClose: () => void;
  onSubmit: () => void;
  children: ReactNode;
}) {
  const titleId = useId();
  const descriptionId = useId();
  useMaskOpenerDialog();
  const focusTrap = useDialogFocusTrap(
    true,
    '[data-policy-waiver-initial-focus]',
  );
  useEscapeToClose(onClose, { canClose: !busy, priority: 150 });

  const dialog = (
    <div className="fixed inset-0 z-[160] flex items-center justify-center bg-black/60 p-4">
      <div
        ref={focusTrap.dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        tabIndex={-1}
        onKeyDown={focusTrap.onKeyDown}
        className="flex max-h-[90vh] w-full max-w-3xl flex-col overflow-hidden rounded-xl border border-surface-200 bg-white shadow-2xl dark:border-surface-700 dark:bg-surface-900"
        data-testid="policy-waiver-dialog"
      >
        <header className="flex items-start justify-between gap-3 border-b border-surface-200 px-5 py-4 dark:border-surface-700">
          <div>
            <h3
              id={titleId}
              className="flex items-center gap-2 text-lg font-semibold text-surface-900 dark:text-white"
            >
              {icon}
              {title}
            </h3>
            <p
              id={descriptionId}
              className="mt-1 text-sm text-surface-500 dark:text-surface-400"
            >
              {description}
            </p>
          </div>
          <button
            type="button"
            data-policy-waiver-initial-focus
            disabled={busy}
            onClick={onClose}
            aria-label="Close waiver dialog"
            className="rounded-lg p-1.5 text-surface-400 hover:bg-surface-100 hover:text-surface-600 disabled:opacity-40 dark:hover:bg-surface-800"
          >
            <X size={18} aria-hidden="true" />
          </button>
        </header>
        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-5 py-4">
          {children}
          {error && (
            <p
              role="alert"
              className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700 dark:border-red-800 dark:bg-red-950/30 dark:text-red-200"
            >
              {error}
            </p>
          )}
        </div>
        <footer className="flex justify-end gap-2 border-t border-surface-200 px-5 py-4 dark:border-surface-700">
          <button
            type="button"
            disabled={busy}
            onClick={onClose}
            className="btn btn-secondary"
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={!valid || busy}
            onClick={onSubmit}
            className="btn btn-primary disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busy ? submittingLabel : submitLabel}
          </button>
        </footer>
      </div>
    </div>
  );
  return typeof document === 'undefined'
    ? dialog
    : createPortal(dialog, document.body);
}

export function PolicyWaiverRequestDialog({
  boardId,
  finding,
  onClose,
  onCompleted,
}: {
  boardId: string;
  finding: SemanticFindingDetail;
  onClose: () => void;
  onCompleted: (result: PolicyWaiverMutationResult) => void | Promise<void>;
}) {
  const api = usePolicyGovernanceApi();
  const permissions = usePermissions(boardId);
  const [justification, setJustification] = useState('');
  const [evidence, setEvidence] = useState(
    () => draftsFromEvidence(finding.evidence_refs),
  );
  const [expiry, setExpiry] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const controllerRef = useRef<AbortController | null>(null);
  const requestRef = useRef(0);
  const identityRef = useRef(newDialogIdentity('semantic-waiver-request'));

  useEffect(() => () => controllerRef.current?.abort(), []);

  const evidenceRefs = useMemo(
    () => parseSemanticEvidenceDrafts(evidence),
    [evidence],
  );
  const expiresAt = useMemo(() => toIsoTimestamp(expiry), [expiry]);
  const expiryValid = (
    !expiry.trim()
    || (
      expiresAt !== null
      && Date.parse(expiresAt) > Date.now()
    )
  );
  const authorityReady = (
    !permissions.isLoading
    && !permissions.error
    && !permissions.ownerReviewRequired
  );
  const canRequest = (
    authorityReady
    && permissions.has('guidelines.waiver.request')
  );
  const valid = (
    canRequest
    && justification.trim().length > 0
    && evidenceRefs !== null
    && expiryValid
  );

  const submit = async () => {
    if (!valid || !evidenceRefs || busy) return;
    const signature = JSON.stringify({
      boardId,
      metricResultId: finding.metric_result_id,
      findingId: finding.finding_id,
      receiptId: finding.receipt_id,
      justification: justification.trim(),
      evidenceRefs,
      expiresAt,
    });
    if (identityRef.current.signature !== signature) {
      identityRef.current = {
        ...newDialogIdentity('semantic-waiver-request'),
        signature,
      };
    }
    const requestId = requestRef.current + 1;
    requestRef.current = requestId;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setBusy(true);
    setError(null);
    try {
      const response = await api.requestSemanticMetricWaiver(
        boardId,
        {
          metric_result_id: finding.metric_result_id,
          finding_id: finding.finding_id,
          receipt_id: finding.receipt_id,
          justification: justification.trim(),
          evidence_refs: evidenceRefs,
          expires_at: expiresAt,
          idempotency_key: identityRef.current.idempotencyKey,
        },
        controller.signal,
      );
      if (controller.signal.aborted || requestId !== requestRef.current) {
        return;
      }
      await onCompleted(parseRequestedSemanticWaiverResponse(response));
    } catch (caught) {
      if (controller.signal.aborted || requestId !== requestRef.current) {
        return;
      }
      setError(policyWaiverErrorMessage(caught));
    } finally {
      if (requestId === requestRef.current) setBusy(false);
    }
  };

  const authorityError = permissions.error
    ? 'Permission status is unavailable. The request action fails closed.'
    : permissions.ownerReviewRequired
      ? 'Owner review is required before waiver requests are available.'
      : !permissions.isLoading && !canRequest
        ? 'guidelines.waiver.request is not granted.'
        : null;

  return (
    <DialogFrame
      title="Request semantic metric waiver"
      description="The request remains ineffective until an independent authorized reviewer approves this exact metric result."
      icon={(
        <FileWarning
          size={19}
          className="text-amber-500"
          aria-hidden="true"
        />
      )}
      busy={busy}
      valid={valid}
      submitLabel="Request waiver"
      submittingLabel="Requesting…"
      error={error}
      onClose={onClose}
      onSubmit={() => void submit()}
    >
      <WaiverScope finding={finding} />
      <label className="block text-xs font-semibold text-surface-700 dark:text-surface-200">
        Justification
        <textarea
          value={justification}
          disabled={busy}
          rows={4}
          onChange={(event) => setJustification(event.target.value)}
          placeholder="Explain why this exact failed metric may be waived."
          className="mt-1 w-full rounded-lg border border-surface-300 bg-white px-3 py-2 text-sm dark:border-surface-700 dark:bg-surface-950"
        />
      </label>
      <EvidenceEditor
        drafts={evidence}
        busy={busy}
        onChange={setEvidence}
      />
      <label className="block text-xs font-semibold text-surface-700 dark:text-surface-200">
        Expiry (optional)
        <input
          type="datetime-local"
          value={expiry}
          disabled={busy}
          onChange={(event) => setExpiry(event.target.value)}
          className="mt-1 block w-full rounded-lg border border-surface-300 bg-white px-3 py-2 text-sm dark:border-surface-700 dark:bg-surface-950"
        />
      </label>
      <p className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800 dark:border-amber-800 dark:bg-amber-950/30 dark:text-amber-200">
        <AlertTriangle
          size={14}
          className="mt-0.5 shrink-0"
          aria-hidden="true"
        />
        The server verifies the metric result, finding, receipt, guideline
        revision and subject version. A later semantic change can make this
        waiver stale.
      </p>
      {authorityError && (
        <p role="alert" className="text-xs text-red-700 dark:text-red-300">
          {authorityError}
        </p>
      )}
    </DialogFrame>
  );
}

const ACTION_PRESENTATION: Record<
  PolicyWaiverAction,
  {
    title: string;
    description: string;
    submit: string;
    submitting: string;
    permission: string;
    icon: ReactNode;
  }
> = {
  approve: {
    title: 'Approve semantic metric waiver',
    description:
      'Approval makes the current exact metric exception effective.',
    submit: 'Approve waiver',
    submitting: 'Approving…',
    permission: 'guidelines.waiver.review',
    icon: (
      <CheckCircle2
        size={19}
        className="text-emerald-500"
        aria-hidden="true"
      />
    ),
  },
  reject: {
    title: 'Reject semantic metric waiver',
    description:
      'Rejection records the independent review without granting an exception.',
    submit: 'Reject waiver',
    submitting: 'Rejecting…',
    permission: 'guidelines.waiver.review',
    icon: (
      <XCircle size={19} className="text-red-500" aria-hidden="true" />
    ),
  },
  revoke: {
    title: 'Revoke semantic metric waiver',
    description:
      'Revocation removes the approved exception and remains append-only evidence.',
    submit: 'Revoke waiver',
    submitting: 'Revoking…',
    permission: 'guidelines.waiver.revoke',
    icon: <Ban size={19} className="text-red-500" aria-hidden="true" />,
  },
  revalidate: {
    title: 'Revalidate semantic metric waiver',
    description:
      'Revalidation checks the immutable anchor and currentness at a new evaluation instant.',
    submit: 'Revalidate waiver',
    submitting: 'Revalidating…',
    permission: 'guidelines.waiver.revalidate',
    icon: (
      <RefreshCw
        size={19}
        className="text-blue-500"
        aria-hidden="true"
      />
    ),
  },
};

export function PolicyWaiverActionDialog({
  boardId,
  evaluatedAt,
  waiver,
  action,
  onClose,
  onCompleted,
}: {
  boardId: string;
  evaluatedAt: string;
  waiver: SemanticWaiverFull;
  action: PolicyWaiverAction;
  onClose: () => void;
  onCompleted: (result: PolicyWaiverMutationResult) => void | Promise<void>;
}) {
  const api = usePolicyGovernanceApi();
  const permissions = usePermissions(boardId);
  const presentation = ACTION_PRESENTATION[action];
  const [reason, setReason] = useState('');
  const [evidence, setEvidence] = useState<SemanticEvidenceDraft[]>([
    emptySemanticEvidenceDraft(),
  ]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [currentWaiver, setCurrentWaiver] =
    useState<SemanticWaiverFull>(waiver);
  const controllerRef = useRef<AbortController | null>(null);
  const requestRef = useRef(0);
  const identityRef = useRef(
    newDialogIdentity(`semantic-waiver-${action}`),
  );

  useEffect(() => () => controllerRef.current?.abort(), []);

  const evidenceRefs = useMemo(
    () => parseSemanticEvidenceDrafts(evidence),
    [evidence],
  );
  const authorityReady = (
    !permissions.isLoading
    && !permissions.error
    && !permissions.ownerReviewRequired
  );
  const canAct = (
    authorityReady
    && permissions.has(presentation.permission)
  );
  const actionApplies = (
    action === 'approve' || action === 'reject'
      ? currentWaiver.status === 'requested'
      : action === 'revoke'
        ? currentWaiver.status === 'approved'
        : (
          currentWaiver.status === 'approved'
          || currentWaiver.status === 'expired'
          || currentWaiver.status === 'revoked'
        )
  );
  const needsReviewEvidence = action !== 'revalidate';
  const valid = (
    canAct
    && actionApplies
    && (
      !needsReviewEvidence
      || (
        reason.trim().length > 0
        && evidenceRefs !== null
      )
    )
  );

  const submit = async () => {
    if (
      !valid
      || busy
      || (needsReviewEvidence && evidenceRefs === null)
    ) {
      return;
    }
    const actionEvaluatedAt = new Date().toISOString();
    const signature = JSON.stringify({
      boardId,
      waiverId: currentWaiver.waiver_id,
      expectedRevision: currentWaiver.waiver_revision,
      action,
      reason: reason.trim(),
      evidenceRefs,
      ...(action === 'revalidate'
        ? { evaluatedAt: actionEvaluatedAt }
        : {}),
    });
    if (identityRef.current.signature !== signature) {
      identityRef.current = {
        ...newDialogIdentity(`semantic-waiver-${action}`),
        signature,
      };
    }
    const requestId = requestRef.current + 1;
    requestRef.current = requestId;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setBusy(true);
    setError(null);
    try {
      let result: PolicyWaiverMutationResult;
      if (action === 'approve' || action === 'reject') {
        const response = await api.reviewSemanticMetricWaiver(
          boardId,
          currentWaiver.waiver_id,
          {
            decision: action,
            reason: reason.trim(),
            evidence_refs: evidenceRefs as NonNullable<typeof evidenceRefs>,
            expected_waiver_revision: currentWaiver.waiver_revision,
            idempotency_key: identityRef.current.idempotencyKey,
          },
          controller.signal,
        );
        result = parseReviewedSemanticWaiverResponse(response, {
          waiverId: currentWaiver.waiver_id,
          previousRevision: currentWaiver.waiver_revision,
          action,
        });
      } else if (action === 'revoke') {
        const response = await api.revokeSemanticMetricWaiver(
          boardId,
          currentWaiver.waiver_id,
          {
            reason: reason.trim(),
            evidence_refs: evidenceRefs as NonNullable<typeof evidenceRefs>,
            expected_waiver_revision: currentWaiver.waiver_revision,
            idempotency_key: identityRef.current.idempotencyKey,
          },
          controller.signal,
        );
        result = parseRevokedSemanticWaiverResponse(response, {
          waiverId: currentWaiver.waiver_id,
          previousRevision: currentWaiver.waiver_revision,
        });
      } else {
        const response = await api.revalidateSemanticMetricWaiver(
          boardId,
          currentWaiver.waiver_id,
          {
            expected_waiver_revision: currentWaiver.waiver_revision,
            evaluated_at: actionEvaluatedAt,
            idempotency_key: identityRef.current.idempotencyKey,
          },
          controller.signal,
        );
        result = parseRevalidatedSemanticWaiverResponse(response, {
          waiverId: currentWaiver.waiver_id,
          previousRevision: currentWaiver.waiver_revision,
        });
      }
      if (controller.signal.aborted || requestId !== requestRef.current) {
        return;
      }
      await onCompleted(result);
    } catch (caught) {
      if (controller.signal.aborted || requestId !== requestRef.current) {
        return;
      }
      if (
        caught instanceof PolicyGovernanceApiError
        && (caught.status === 409 || caught.kind === 'conflict')
      ) {
        try {
          const response = await api.getSemanticMetricWaiver(
            boardId,
            currentWaiver.waiver_id,
            {
              evaluatedAt,
              projection: 'full',
              signal: controller.signal,
            },
          );
          const refreshed = parseSemanticWaiverHeadResponse(response, {
            boardId,
            evaluatedAt,
            waiverId: currentWaiver.waiver_id,
            findingId: currentWaiver.finding_id,
            metricResultId: currentWaiver.metric_result_id,
          });
          if (
            controller.signal.aborted
            || requestId !== requestRef.current
          ) {
            return;
          }
          setCurrentWaiver(refreshed);
          identityRef.current = newDialogIdentity(
            `semantic-waiver-${action}`,
          );
          setError(
            'The waiver changed while this action was open. '
            + `Authority was refreshed to revision `
            + `${refreshed.waiver_revision}; review it before retrying.`,
          );
          return;
        } catch (refreshError) {
          if (controller.signal.aborted) return;
          setError(
            `${policyWaiverErrorMessage(caught)} Authority refresh failed: `
            + policyWaiverErrorMessage(refreshError),
          );
          return;
        }
      }
      setError(policyWaiverErrorMessage(caught));
    } finally {
      if (requestId === requestRef.current) setBusy(false);
    }
  };

  const separated = (
    action === 'approve'
    || action === 'reject'
    || action === 'revalidate'
  );
  const authorityError = permissions.error
    ? 'Permission status is unavailable. This action fails closed.'
    : permissions.ownerReviewRequired
      ? 'Owner review is required before this action is available.'
      : !permissions.isLoading && !canAct
        ? `${presentation.permission} is not granted.`
        : null;

  return (
    <DialogFrame
      title={presentation.title}
      description={presentation.description}
      icon={presentation.icon}
      busy={busy}
      valid={valid}
      submitLabel={presentation.submit}
      submittingLabel={presentation.submitting}
      error={error}
      onClose={onClose}
      onSubmit={() => void submit()}
    >
      <WaiverScope waiver={currentWaiver} />
      <div className="grid gap-2 rounded-lg border border-surface-200 bg-surface-50 p-3 text-xs dark:border-surface-700 dark:bg-surface-950/40 sm:grid-cols-3">
        <p>
          <span className="font-semibold">Requester:</span>
          {' '}{currentWaiver.requested_by}
        </p>
        <p>
          <span className="font-semibold">Current revision:</span>
          {' '}{currentWaiver.waiver_revision}
        </p>
        <p>
          <span className="font-semibold">Expiry:</span>
          {' '}
          {currentWaiver.expires_at
            ? formatPolicyTimestamp(currentWaiver.expires_at)
            : 'No scheduled expiry'}
        </p>
      </div>
      {separated && (
        <p className="flex items-start gap-2 rounded-lg border border-blue-200 bg-blue-50 p-3 text-xs text-blue-800 dark:border-blue-800 dark:bg-blue-950/30 dark:text-blue-200">
          <ShieldCheck
            size={14}
            className="mt-0.5 shrink-0"
            aria-hidden="true"
          />
          Reviewer separation is enforced against both the requester and the
          agent that authored the semantic assessment.
        </p>
      )}
      {needsReviewEvidence ? (
        <>
          <label className="block text-xs font-semibold text-surface-700 dark:text-surface-200">
            {action === 'revoke' ? 'Revocation reason' : 'Review reason'}
            <textarea
              value={reason}
              disabled={busy}
              rows={4}
              onChange={(event) => setReason(event.target.value)}
              placeholder="Record the auditable rationale for this event."
              className="mt-1 w-full rounded-lg border border-surface-300 bg-white px-3 py-2 text-sm dark:border-surface-700 dark:bg-surface-950"
            />
          </label>
          <EvidenceEditor
            drafts={evidence}
            busy={busy}
            onChange={setEvidence}
          />
        </>
      ) : (
        <p className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800 dark:border-amber-800 dark:bg-amber-950/30 dark:text-amber-200">
          <RefreshCw
            size={14}
            className="mt-0.5 shrink-0"
            aria-hidden="true"
          />
          The server will evaluate the immutable anchor, subject, guideline,
          binding and metric result at submission time. The response is
          authoritative and may keep the waiver expired, stale or revoked.
        </p>
      )}
      {authorityError && (
        <p role="alert" className="text-xs text-red-700 dark:text-red-300">
          {authorityError}
        </p>
      )}
      {!actionApplies && (
        <p role="alert" className="text-xs text-red-700 dark:text-red-300">
          This action is not valid for the authoritative{' '}
          {formatPolicyToken(currentWaiver.status)} status.
        </p>
      )}
    </DialogFrame>
  );
}
