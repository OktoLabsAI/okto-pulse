import {
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
  Clock3,
  FileWarning,
  RefreshCw,
  ShieldCheck,
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
  PolicyComplianceFindingDetail,
  PolicyWaiver,
  PolicyWaiverEvent,
  PolicyWaiverEventType,
  PolicyWaiverListItem,
} from '@/types/policy-governance';

import {
  createPolicyUiId,
  formatPolicyToken,
  formatPolicyTimestamp,
} from './policyComplianceModel';
import {
  isPolicyWaiverForExpectedScope,
  parsePolicyEvidenceRefs,
  policyWaiverErrorMessage,
  validatedPolicyWaiverMutation,
} from './policyWaiverModel';

export interface PolicyWaiverMutationResult {
  waiver: PolicyWaiver;
  event: PolicyWaiverEvent;
}

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

/**
 * The waiver dialogs are portals so they are not descendants of an existing
 * modal. While mounted, the opener's parent dialog is removed from the
 * accessibility tree and restored exactly on close.
 */
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

function WaiverScope({
  finding,
  waiver,
}: {
  finding?: PolicyComplianceFindingDetail;
  waiver?: PolicyWaiverListItem | PolicyWaiver;
}) {
  const source = finding ?? waiver;
  if (!source) return null;
  return (
    <section
      className="rounded-lg border border-surface-200 bg-surface-50 p-3 text-xs dark:border-surface-700 dark:bg-surface-950/40"
      data-testid="policy-waiver-exact-scope"
    >
      <h4 className="font-semibold text-surface-800 dark:text-surface-100">
        Server-owned exact scope
      </h4>
      <p className="mt-1 text-surface-600 dark:text-surface-300">
        These identities are read-only. The server resolves and verifies them
        from the selected finding.
      </p>
      <dl className="mt-2 grid gap-x-4 gap-y-1 sm:grid-cols-2">
        <div>
          <dt className="inline font-semibold">Subject: </dt>
          <dd className="inline break-all">
            {formatPolicyToken(source.subject.entity_type)}
            {' · '}
            {source.subject.subject_id}
            {' · v'}
            {source.subject.subject_version}
          </dd>
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
          <dt className="inline font-semibold">Guideline: </dt>
          <dd className="inline break-all">{source.guideline_id}</dd>
        </div>
        <div>
          <dt className="inline font-semibold">Revision: </dt>
          <dd className="inline break-all">{source.revision_id}</dd>
        </div>
        <div>
          <dt className="inline font-semibold">Rule: </dt>
          <dd className="inline break-all">{source.rule_id}</dd>
        </div>
      </dl>
    </section>
  );
}

function EvidenceFields({
  reasonLabel,
  reason,
  evidence,
  expiry,
  expiryLabel,
  busy,
  onReasonChange,
  onEvidenceChange,
  onExpiryChange,
}: {
  reasonLabel: string;
  reason: string;
  evidence: string;
  expiry?: string;
  expiryLabel?: string;
  busy: boolean;
  onReasonChange: (value: string) => void;
  onEvidenceChange: (value: string) => void;
  onExpiryChange?: (value: string) => void;
}) {
  return (
    <>
      <label className="block text-xs font-semibold text-surface-700 dark:text-surface-200">
        {reasonLabel}
        <textarea
          value={reason}
          disabled={busy}
          required
          rows={4}
          onChange={(event) => onReasonChange(event.target.value)}
          placeholder="Record the auditable rationale for this lifecycle event."
          className="mt-1 w-full rounded-lg border border-surface-300 bg-white px-3 py-2 text-sm dark:border-surface-700 dark:bg-surface-950"
        />
      </label>
      <label className="block text-xs font-semibold text-surface-700 dark:text-surface-200">
        Evidence references
        <textarea
          value={evidence}
          disabled={busy}
          required
          rows={3}
          onChange={(event) => onEvidenceChange(event.target.value)}
          placeholder={'One immutable reference per line\nticket://...\nreceipt://...'}
          className="mt-1 w-full rounded-lg border border-surface-300 bg-white px-3 py-2 font-mono text-xs dark:border-surface-700 dark:bg-surface-950"
        />
        <span className="mt-1 block font-normal text-surface-500">
          Empty lines and duplicate references are removed.
        </span>
      </label>
      {expiry !== undefined && expiryLabel && onExpiryChange && (
        <label className="block text-xs font-semibold text-surface-700 dark:text-surface-200">
          {expiryLabel}
          <input
            type="datetime-local"
            value={expiry}
            disabled={busy}
            required
            onChange={(event) => onExpiryChange(event.target.value)}
            className="mt-1 block w-full rounded-lg border border-surface-300 bg-white px-3 py-2 text-sm dark:border-surface-700 dark:bg-surface-950"
          />
        </label>
      )}
    </>
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
  icon: React.ReactNode;
  busy: boolean;
  valid: boolean;
  submitLabel: string;
  submittingLabel: string;
  error: string | null;
  onClose: () => void;
  onSubmit: () => void;
  children: React.ReactNode;
}) {
  const titleId = useId();
  const descriptionId = useId();
  useMaskOpenerDialog();
  const focusTrap = useDialogFocusTrap(
    true,
    '[data-policy-waiver-initial-focus]',
  );
  useEscapeToClose(onClose, {
    canClose: !busy,
    priority: 150,
  });

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
        className="flex max-h-[90vh] w-full max-w-2xl flex-col overflow-hidden rounded-xl border border-surface-200 bg-white shadow-2xl dark:border-surface-700 dark:bg-surface-900"
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
  finding: PolicyComplianceFindingDetail;
  onClose: () => void;
  onCompleted: (result: PolicyWaiverMutationResult) => void | Promise<void>;
}) {
  const api = usePolicyGovernanceApi();
  const permissions = usePermissions(boardId);
  const [justification, setJustification] = useState('');
  const [evidence, setEvidence] = useState(
    finding.evidence_refs.join('\n'),
  );
  const [expiry, setExpiry] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const controllerRef = useRef<AbortController | null>(null);
  const requestRef = useRef(0);
  const identityRef = useRef(newDialogIdentity('policy-waiver-request'));

  useEffect(
    () => () => controllerRef.current?.abort(),
    [],
  );

  const evidenceRefs = useMemo(
    () => parsePolicyEvidenceRefs(evidence),
    [evidence],
  );
  const expiresAt = useMemo(() => toIsoTimestamp(expiry), [expiry]);
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
    && expiresAt !== null
    && new Date(expiresAt).getTime() > Date.now()
  );

  const submit = async () => {
    if (!valid || !evidenceRefs || !expiresAt || busy) return;
    const signature = JSON.stringify({
      boardId,
      findingId: finding.finding_id,
      justification: justification.trim(),
      evidenceRefs,
      expiresAt,
    });
    if (identityRef.current.signature !== signature) {
      identityRef.current = {
        ...newDialogIdentity('policy-waiver-request'),
        signature,
      };
    }
    const identity = identityRef.current;
    const requestId = requestRef.current + 1;
    requestRef.current = requestId;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setBusy(true);
    setError(null);
    try {
      const response = await api.requestPolicyWaiver(
        boardId,
        {
          finding_id: finding.finding_id,
          justification: justification.trim(),
          evidence_refs: evidenceRefs,
          expires_at: expiresAt,
          idempotency_key: identity.idempotencyKey,
        },
        controller.signal,
      );
      if (controller.signal.aborted || requestId !== requestRef.current) {
        return;
      }
      const result = validatedPolicyWaiverMutation(response, {
        boardId,
        findingId: finding.finding_id,
        previousRevision: 0,
        eventType: 'request',
        reason: justification.trim(),
        evidenceRefs,
        expiresAt,
      });
      await onCompleted(result);
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
      title="Request governed waiver"
      description="A request is append-only evidence. It is not effective until an independent authorized reviewer approves it."
      icon={<FileWarning size={19} className="text-amber-500" aria-hidden="true" />}
      busy={busy}
      valid={valid}
      submitLabel="Request waiver"
      submittingLabel="Requesting…"
      error={error}
      onClose={onClose}
      onSubmit={() => void submit()}
    >
      <WaiverScope finding={finding} />
      <EvidenceFields
        reasonLabel="Justification"
        reason={justification}
        evidence={evidence}
        expiry={expiry}
        expiryLabel="Requested expiry"
        busy={busy}
        onReasonChange={setJustification}
        onEvidenceChange={setEvidence}
        onExpiryChange={setExpiry}
      />
      <p className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800 dark:border-amber-800 dark:bg-amber-950/30 dark:text-amber-200">
        <AlertTriangle size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
        The backend verifies that the exact rule is waivable and that this
        finding, receipt, guideline revision and subject version are current.
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
    reasonLabel: string;
    eventType: PolicyWaiverEventType;
    permission: string;
    icon: React.ReactNode;
  }
> = {
  approve: {
    title: 'Approve governed waiver',
    description:
      'Approval makes this exact, current scope effective until its recorded expiry.',
    submit: 'Approve waiver',
    submitting: 'Approving…',
    reasonLabel: 'Review reason',
    eventType: 'approve',
    permission: 'guidelines.waiver.review',
    icon: <CheckCircle2 size={19} className="text-emerald-500" aria-hidden="true" />,
  },
  reject: {
    title: 'Reject governed waiver',
    description:
      'Rejection records the independent review without changing the finding scope.',
    submit: 'Reject waiver',
    submitting: 'Rejecting…',
    reasonLabel: 'Review reason',
    eventType: 'reject',
    permission: 'guidelines.waiver.review',
    icon: <XCircle size={19} className="text-red-500" aria-hidden="true" />,
  },
  revoke: {
    title: 'Revoke governed waiver',
    description:
      'Revocation immediately removes the approved exception and remains in immutable history.',
    submit: 'Revoke waiver',
    submitting: 'Revoking…',
    reasonLabel: 'Revocation reason',
    eventType: 'revoke',
    permission: 'guidelines.waiver.revoke',
    icon: <Ban size={19} className="text-red-500" aria-hidden="true" />,
  },
  revalidate: {
    title: 'Revalidate governed waiver',
    description:
      'Revalidation is a new independent, append-only approval event with a later expiry.',
    submit: 'Revalidate waiver',
    submitting: 'Revalidating…',
    reasonLabel: 'Revalidation reason',
    eventType: 'revalidate',
    permission: 'guidelines.waiver.revalidate',
    icon: <RefreshCw size={19} className="text-blue-500" aria-hidden="true" />,
  },
};

export function PolicyWaiverActionDialog({
  boardId,
  waiver,
  action,
  onClose,
  onCompleted,
}: {
  boardId: string;
  waiver: PolicyWaiverListItem;
  action: PolicyWaiverAction;
  onClose: () => void;
  onCompleted: (result: PolicyWaiverMutationResult) => void | Promise<void>;
}) {
  const api = usePolicyGovernanceApi();
  const permissions = usePermissions(boardId);
  const presentation = ACTION_PRESENTATION[action];
  const [reason, setReason] = useState('');
  const [evidence, setEvidence] = useState('');
  const [expiry, setExpiry] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [currentWaiver, setCurrentWaiver] =
    useState<PolicyWaiverListItem | PolicyWaiver>(waiver);
  const controllerRef = useRef<AbortController | null>(null);
  const requestRef = useRef(0);
  const identityRef = useRef(newDialogIdentity(`policy-waiver-${action}`));

  useEffect(
    () => () => controllerRef.current?.abort(),
    [],
  );

  const evidenceRefs = useMemo(
    () => parsePolicyEvidenceRefs(evidence),
    [evidence],
  );
  const newExpiresAt = useMemo(
    () => action === 'revalidate' ? toIsoTimestamp(expiry) : null,
    [action, expiry],
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
    (action === 'approve' || action === 'reject')
      ? currentWaiver.status === 'requested'
      : action === 'revoke'
        ? currentWaiver.status === 'approved'
        : (
          currentWaiver.status === 'approved'
          || (
            currentWaiver.status === 'expired'
            && currentWaiver.expire_reason_code === 'scheduled_expiry'
          )
        )
  );
  const expiryValid = (
    action !== 'revalidate'
    || (
      newExpiresAt !== null
      && new Date(newExpiresAt).getTime() > Date.now()
      && new Date(newExpiresAt).getTime()
        > new Date(currentWaiver.expires_at).getTime()
    )
  );
  const valid = (
    canAct
    && actionApplies
    && reason.trim().length > 0
    && evidenceRefs !== null
    && expiryValid
  );

  const submit = async () => {
    if (!valid || !evidenceRefs || busy) return;
    const signature = JSON.stringify({
      boardId,
      waiverId: currentWaiver.waiver_id,
      expectedRevision: currentWaiver.waiver_revision,
      action,
      reason: reason.trim(),
      evidenceRefs,
      newExpiresAt,
    });
    if (identityRef.current.signature !== signature) {
      identityRef.current = {
        ...newDialogIdentity(`policy-waiver-${action}`),
        signature,
      };
    }
    const identity = identityRef.current;
    const shared = {
      reason: reason.trim(),
      evidence_refs: evidenceRefs,
      expected_waiver_revision: currentWaiver.waiver_revision,
      idempotency_key: identity.idempotencyKey,
    };
    const requestId = requestRef.current + 1;
    requestRef.current = requestId;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setBusy(true);
    setError(null);
    try {
      const response = action === 'approve' || action === 'reject'
        ? await api.reviewPolicyWaiver(
            boardId,
            currentWaiver.waiver_id,
            {
              ...shared,
              decision: action,
            },
            controller.signal,
          )
        : action === 'revoke'
          ? await api.revokePolicyWaiver(
              boardId,
              currentWaiver.waiver_id,
              shared,
              controller.signal,
            )
          : await api.revalidatePolicyWaiver(
              boardId,
              currentWaiver.waiver_id,
              {
                ...shared,
                new_expires_at: newExpiresAt as string,
              },
              controller.signal,
            );
      if (controller.signal.aborted || requestId !== requestRef.current) {
        return;
      }
      const result = validatedPolicyWaiverMutation(response, {
        boardId,
        waiverId: currentWaiver.waiver_id,
        findingId: currentWaiver.finding_id,
        previousRevision: currentWaiver.waiver_revision,
        eventType: presentation.eventType,
        reason: reason.trim(),
        evidenceRefs,
        ...(action === 'revalidate' && newExpiresAt
          ? { expiresAt: newExpiresAt }
          : {}),
      });
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
          const response = await api.getPolicyWaiver(
            boardId,
            currentWaiver.waiver_id,
            controller.signal,
          );
          if (
            controller.signal.aborted
            || requestId !== requestRef.current
            || !isPolicyWaiverForExpectedScope(response?.waiver, {
              boardId,
              waiverId: currentWaiver.waiver_id,
              findingId: currentWaiver.finding_id,
            })
          ) {
            if (!controller.signal.aborted) {
              throw new Error(
                'The current waiver authority could not be verified.',
              );
            }
            return;
          }
          setCurrentWaiver(response.waiver);
          identityRef.current = newDialogIdentity(
            `policy-waiver-${action}`,
          );
          setError(
            'The waiver changed while this action was open. '
            + `Authority was refreshed to revision `
            + `${response.waiver.waiver_revision}; review it before retrying.`,
          );
          return;
        } catch (refreshError) {
          if (controller.signal.aborted) return;
          setError(
            `${policyWaiverErrorMessage(caught)} `
            + `Authority refresh failed: `
            + `${policyWaiverErrorMessage(refreshError)}`,
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
          <span className="font-semibold">Current expiry:</span>
          {' '}{formatPolicyTimestamp(currentWaiver.expires_at)}
        </p>
      </div>
      {separated && (
        <p className="flex items-start gap-2 rounded-lg border border-blue-200 bg-blue-50 p-3 text-xs text-blue-800 dark:border-blue-800 dark:bg-blue-950/30 dark:text-blue-200">
          <ShieldCheck size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
          Reviewer separation is enforced by the server. The requester cannot
          perform this action, even when their preset grants the capability.
        </p>
      )}
      <EvidenceFields
        reasonLabel={presentation.reasonLabel}
        reason={reason}
        evidence={evidence}
        expiry={action === 'revalidate' ? expiry : undefined}
        expiryLabel={
          action === 'revalidate' ? 'New later expiry' : undefined
        }
        busy={busy}
        onReasonChange={setReason}
        onEvidenceChange={setEvidence}
        onExpiryChange={action === 'revalidate' ? setExpiry : undefined}
      />
      {action === 'revalidate' && (
        <p className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800 dark:border-amber-800 dark:bg-amber-950/30 dark:text-amber-200">
          <Clock3 size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
          The new expiry must be later than both now and the existing expiry.
          Structurally invalidated waivers remain terminal.
        </p>
      )}
      {authorityError && (
        <p role="alert" className="text-xs text-red-700 dark:text-red-300">
          {authorityError}
        </p>
      )}
      {!actionApplies && (
        <p role="alert" className="text-xs text-red-700 dark:text-red-300">
          This action is no longer valid for the authoritative
          {' '}{formatPolicyToken(currentWaiver.status)} status.
        </p>
      )}
    </DialogFrame>
  );
}
