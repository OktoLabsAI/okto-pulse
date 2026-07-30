import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  CircleOff,
  FileWarning,
  Loader2,
  RefreshCw,
  ShieldAlert,
  X,
} from 'lucide-react';

import { CollapsibleEvidenceSection } from '@/components/shared/CollapsibleEvidenceSection';
import { CursorCollectionControls } from '@/components/shared/CursorCollectionControls';
import { useDialogFocusTrap } from '@/hooks/useDialogFocusTrap';
import { useEscapeToClose } from '@/hooks/useEscapeToClose';
import { ContextualHelpLink } from '@/components/help';
import { useOpaqueCursorCollection } from '@/hooks/useOpaqueCursorCollection';
import { usePermissions } from '@/hooks/usePermissions';
import { usePolicyGovernanceApi } from '@/services/policy-governance-api';
import type {
  GuidelineAdoptionResponse,
  GuidelineEnforcement,
  GuidelineImpactPageItem,
  GuidelineImpactReceipt,
  GuidelineRevisionAuthorityResponse,
} from '@/types/policy-governance';

import {
  GUIDELINE_IMPACT_KIND_LABEL,
  MAX_GUIDELINE_PRIORITY,
  classifyGuidelineImpactCursorError,
  countGuidelineImpactItems,
  createGuidelinePolicyClientId,
  guidelineImpactErrorMessage,
  isGuidelineAdoptionResponseForReceipt,
  isGuidelineImpactConflict,
  isGuidelineImpactReceiptForPreview,
  isGuidelineRevisionAuthorityForTarget,
  validatedGuidelineImpactPage,
} from './guidelineImpactModel';

const IMPACT_PAGE_SIZE = 50;

type RevisionState =
  | {
      scope: string;
      status: 'idle' | 'loading';
      authority: null;
      error: null;
    }
  | {
      scope: string;
      status: 'ready';
      authority: GuidelineRevisionAuthorityResponse;
      error: null;
    }
  | {
      scope: string;
      status: 'error';
      authority: null;
      error: string;
    };

export interface AdoptedGuidelineBindingAuthority {
  bindingId: string;
  bindingRevision: number;
  bindingState: 'active';
  revisionId: string;
  semanticVersion: string;
  revisionDigest: string;
}

export interface GuidelineImpactDialogProps {
  boardId: string;
  guidelineId: string;
  guidelineTitle: string;
  targetRevisionId: string;
  targetSemanticVersion: string;
  adoptedBinding?: AdoptedGuidelineBindingAuthority;
  initialPriority: number;
  initialEnforcement: GuidelineEnforcement;
  onClose: () => void;
  onAdopted: (
    response: GuidelineAdoptionResponse,
  ) => void | Promise<void>;
}

function formatTimestamp(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function updateIsAvailable({
  adoptedBinding,
  targetRevisionId,
  targetSemanticVersion,
}: Pick<
  GuidelineImpactDialogProps,
  | 'adoptedBinding'
  | 'targetRevisionId'
  | 'targetSemanticVersion'
>): boolean {
  if (adoptedBinding) {
    return (
      adoptedBinding.revisionId !== targetRevisionId
      || adoptedBinding.semanticVersion !== targetSemanticVersion
    );
  }
  return true;
}

function ImpactItemRow({ item }: { item: GuidelineImpactPageItem }) {
  return (
    <li
      className="rounded-lg border border-surface-200 bg-white p-3 dark:border-surface-700 dark:bg-surface-900/50"
      data-testid={`guideline-impact-item-${item.impact_item_id}`}
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${
            item.item_kind === 'waiver'
              ? 'bg-amber-100 text-amber-700 dark:bg-amber-400/15 dark:text-amber-200'
              : item.item_kind === 'binding'
                ? 'bg-blue-100 text-blue-700 dark:bg-blue-400/15 dark:text-blue-200'
                : 'bg-surface-100 text-surface-700 dark:bg-surface-800 dark:text-surface-200'
          }`}>
            {GUIDELINE_IMPACT_KIND_LABEL[item.item_kind]}
          </span>
          <p className="mt-1 break-all text-xs font-medium text-surface-800 dark:text-surface-100">
            {item.entity_type} · {item.entity_id}
          </p>
        </div>
        {item.entity_version !== undefined && (
          <span className="text-[11px] text-surface-500">
            Entity v{item.entity_version}
          </span>
        )}
      </div>
      {item.related_id && (
        <p className="mt-2 break-all text-xs text-surface-600 dark:text-surface-300">
          {item.item_kind === 'waiver' ? 'Waiver' : 'Related'}: {item.related_id}
        </p>
      )}
      <p className="mt-2 break-all text-[10px] text-surface-500 dark:text-surface-400">
        Evidence digest {item.details_digest}
      </p>
    </li>
  );
}

export function GuidelineImpactDialog({
  boardId,
  guidelineId,
  guidelineTitle,
  targetRevisionId,
  targetSemanticVersion,
  adoptedBinding,
  initialPriority,
  initialEnforcement,
  onClose,
  onAdopted,
}: GuidelineImpactDialogProps) {
  const api = usePolicyGovernanceApi();
  const permissions = usePermissions(boardId);
  const [priorityInput, setPriorityInput] = useState(
    String(initialPriority),
  );
  const [enforcement, setEnforcement] =
    useState<GuidelineEnforcement>(initialEnforcement);
  const [revisionState, setRevisionState] = useState<RevisionState>({
    scope: '',
    status: 'idle',
    authority: null,
    error: null,
  });
  const [receipt, setReceipt] =
    useState<GuidelineImpactReceipt | null>(null);
  const [receiptSignature, setReceiptSignature] = useState('');
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [conflictMessage, setConflictMessage] = useState<string | null>(null);
  const [adopting, setAdopting] = useState(false);
  const [adoptionError, setAdoptionError] = useState<string | null>(null);
  const [itemsExpanded, setItemsExpanded] = useState(true);

  const previewControllerRef = useRef<AbortController | null>(null);
  const previewRequestRef = useRef(0);
  const previewActiveRef = useRef(false);
  const previewIntentRef = useRef({ signature: '', idempotencyKey: '' });
  const revisionControllerRef = useRef<AbortController | null>(null);
  const revisionRequestRef = useRef(0);
  const adoptionControllerRef = useRef<AbortController | null>(null);
  const adoptionRequestRef = useRef(0);
  const adoptionActiveRef = useRef(false);
  const adoptionIntentRef = useRef({ signature: '', idempotencyKey: '' });

  const priority = Number(priorityInput);
  const priorityValid = (
    /^\d+$/.test(priorityInput)
    && Number.isInteger(priority)
    && priority >= 0
    && priority <= MAX_GUIDELINE_PRIORITY
  );
  const previewSignature = JSON.stringify([
    boardId,
    guidelineId,
    targetRevisionId,
    targetSemanticVersion,
    priorityValid ? priority : null,
    enforcement,
  ]);
  const previewSignatureRef = useRef(previewSignature);
  previewSignatureRef.current = previewSignature;

  const authorityReady = (
    !permissions.isLoading
    && !permissions.error
    && !permissions.ownerReviewRequired
  );
  const canReadRevision = (
    authorityReady
    && permissions.has('guidelines.revisions.read')
  );
  const canPreview = (
    authorityReady
    && permissions.has('guidelines.impact.preview')
  );
  const canAdopt = (
    authorityReady
    && permissions.has('guidelines.adoption.manage')
  );
  const busy = previewLoading || adopting;

  useEscapeToClose(onClose, {
    canClose: !busy,
    priority: 100,
  });
  const focusTrap = useDialogFocusTrap(
    true,
    '[data-guideline-impact-initial-focus]',
  );

  const revisionScope = JSON.stringify([
    boardId,
    guidelineId,
    targetRevisionId,
    targetSemanticVersion,
    canReadRevision,
  ]);

  useEffect(() => {
    const requestId = revisionRequestRef.current + 1;
    revisionRequestRef.current = requestId;
    revisionControllerRef.current?.abort();

    if (!canReadRevision) {
      setRevisionState({
        scope: revisionScope,
        status: 'error',
        authority: null,
        error: permissions.isLoading
          ? 'Checking revision permissions…'
          : permissions.error
            ? 'Revision authority is unavailable.'
            : permissions.ownerReviewRequired
              ? 'Permission lineage requires owner review.'
              : 'You do not have permission to read guideline revisions.',
      });
      return undefined;
    }

    const controller = new AbortController();
    revisionControllerRef.current = controller;
    setRevisionState({
      scope: revisionScope,
      status: 'loading',
      authority: null,
      error: null,
    });
    void api
      .getGuidelineRevision(
        boardId,
        guidelineId,
        targetRevisionId,
        controller.signal,
      )
      .then((response) => {
        if (
          controller.signal.aborted
          || requestId !== revisionRequestRef.current
        ) {
          return;
        }
        if (
          !isGuidelineRevisionAuthorityForTarget(response, {
            guidelineId,
            revisionId: targetRevisionId,
            semanticVersion: targetSemanticVersion,
          })
        ) {
          throw new Error(
            'Guideline revision authority returned a mismatched payload.',
          );
        }
        setRevisionState({
          scope: revisionScope,
          status: 'ready',
          authority: response,
          error: null,
        });
      })
      .catch((error: unknown) => {
        if (
          controller.signal.aborted
          || requestId !== revisionRequestRef.current
        ) {
          return;
        }
        setRevisionState({
          scope: revisionScope,
          status: 'error',
          authority: null,
          error: guidelineImpactErrorMessage(error),
        });
      });
    return () => controller.abort();
  }, [
    api,
    boardId,
    canReadRevision,
    guidelineId,
    permissions.error,
    permissions.isLoading,
    permissions.ownerReviewRequired,
    revisionScope,
    targetRevisionId,
    targetSemanticVersion,
  ]);

  const activeRevisionState = revisionState.scope === revisionScope
    ? revisionState
    : {
        scope: revisionScope,
        status: 'loading' as const,
        authority: null,
        error: null,
      };
  const revisionAuthority = activeRevisionState.status === 'ready'
    ? activeRevisionState.authority
    : null;

  useEffect(() => {
    previewRequestRef.current += 1;
    previewControllerRef.current?.abort();
    previewActiveRef.current = false;
    adoptionRequestRef.current += 1;
    adoptionControllerRef.current?.abort();
    adoptionActiveRef.current = false;
    setReceipt(null);
    setReceiptSignature('');
    setPreviewLoading(false);
    setPreviewError(null);
    setConflictMessage(null);
    setAdopting(false);
    setAdoptionError(null);
  }, [previewSignature]);

  useEffect(
    () => () => {
      previewControllerRef.current?.abort();
      revisionControllerRef.current?.abort();
      adoptionControllerRef.current?.abort();
    },
    [],
  );

  const currentReceipt = (
    receipt !== null
    && receiptSignature === previewSignature
  )
    ? receipt
    : null;
  const currentReceiptId = currentReceipt?.impact_receipt_id ?? null;

  const loadImpactItems = useCallback(
    async (cursor: string | undefined, signal: AbortSignal) => {
      if (!currentReceiptId) {
        throw new Error('A current impact receipt is required.');
      }
      const page = await api.listGuidelineImpactItems(
        boardId,
        guidelineId,
        currentReceiptId,
        {
          limit: IMPACT_PAGE_SIZE,
          projection: 'detail',
          cursor,
          signal,
        },
      );
      return validatedGuidelineImpactPage(page);
    },
    [api, boardId, currentReceiptId, guidelineId],
  );
  const impactItems = useOpaqueCursorCollection<GuidelineImpactPageItem>({
    enabled: Boolean(currentReceiptId && canPreview),
    resetKey: JSON.stringify([
      boardId,
      guidelineId,
      currentReceiptId,
      'detail',
      IMPACT_PAGE_SIZE,
    ]),
    loadPage: loadImpactItems,
    getItemKey: (item) => item.impact_item_id,
    classifyError: classifyGuidelineImpactCursorError,
    duplicateItemMessage:
      'The impact receipt returned a duplicate item identity.',
    repeatedCursorMessage:
      'The impact receipt returned a repeated cursor.',
  });

  const runPreview = useCallback(
    async (fresh: boolean) => {
      if (
        previewActiveRef.current
        || !canPreview
        || !priorityValid
        || revisionAuthority === null
      ) {
        return;
      }
      const signature = previewSignature;
      const requestId = previewRequestRef.current + 1;
      previewRequestRef.current = requestId;
      previewControllerRef.current?.abort();
      const controller = new AbortController();
      previewControllerRef.current = controller;
      previewActiveRef.current = true;

      if (
        fresh
        || previewIntentRef.current.signature !== signature
        || !previewIntentRef.current.idempotencyKey
      ) {
        previewIntentRef.current = {
          signature,
          idempotencyKey:
            createGuidelinePolicyClientId('guideline-impact-preview'),
        };
      }
      const idempotencyKey = previewIntentRef.current.idempotencyKey;
      setPreviewLoading(true);
      setPreviewError(null);
      setConflictMessage(null);
      setAdoptionError(null);
      setReceipt(null);
      setReceiptSignature('');

      try {
        const response = await api.previewGuidelineImpact(
          boardId,
          guidelineId,
          {
            proposed_priority: priority,
            proposed_default_enforcement: enforcement,
            to_revision_id: targetRevisionId,
            idempotency_key: idempotencyKey,
          },
          controller.signal,
        );
        if (
          controller.signal.aborted
          || requestId !== previewRequestRef.current
          || previewSignatureRef.current !== signature
        ) {
          return;
        }
        const authoritativeRevision = revisionAuthority.revision;
        if (
          response.receipt.to_revision_number
            !== authoritativeRevision.revision_number
          || response.receipt.to_revision_digest
            !== authoritativeRevision.content_digest
          || response.receipt.expected_head_revision
            !== authoritativeRevision.revision_number
        ) {
          throw new Error(
            'The latest guideline revision changed. Close this dialog and review the new head before adoption.',
          );
        }
        if (
          !isGuidelineImpactReceiptForPreview(response.receipt, {
            boardId,
            guidelineId,
            revisionId: targetRevisionId,
            revisionNumber: authoritativeRevision.revision_number,
            revisionDigest: authoritativeRevision.content_digest,
            semanticVersion: targetSemanticVersion,
            priority,
            enforcement,
            adoptedBinding: adoptedBinding ?? null,
          })
        ) {
          throw new Error(
            'Impact preview returned a mismatched or malformed receipt.',
          );
        }
        setReceipt(response.receipt);
        setReceiptSignature(signature);
        setItemsExpanded(true);
        adoptionIntentRef.current = {
          signature: '',
          idempotencyKey: '',
        };
      } catch (error: unknown) {
        if (
          controller.signal.aborted
          || requestId !== previewRequestRef.current
          || previewSignatureRef.current !== signature
        ) {
          return;
        }
        if (isGuidelineImpactConflict(error)) {
          setConflictMessage(
            'The board policy changed while the impact was being prepared. Reload the impact before adoption.',
          );
        } else {
          setPreviewError(guidelineImpactErrorMessage(error));
        }
      } finally {
        if (
          !controller.signal.aborted
          && requestId === previewRequestRef.current
          && previewSignatureRef.current === signature
        ) {
          previewActiveRef.current = false;
          setPreviewLoading(false);
        }
      }
    },
    [
      api,
      boardId,
      canPreview,
      adoptedBinding,
      enforcement,
      guidelineId,
      previewSignature,
      priority,
      priorityValid,
      revisionAuthority,
      targetRevisionId,
      targetSemanticVersion,
    ],
  );

  const adopt = useCallback(async () => {
    if (
      adoptionActiveRef.current
      || !canAdopt
      || !currentReceipt
      || previewLoading
    ) {
      return;
    }
    const signature = JSON.stringify([
      currentReceipt.impact_receipt_id,
      currentReceipt.impact_digest,
    ]);
    if (
      adoptionIntentRef.current.signature !== signature
      || !adoptionIntentRef.current.idempotencyKey
    ) {
      adoptionIntentRef.current = {
        signature,
        idempotencyKey:
          createGuidelinePolicyClientId('guideline-adoption'),
      };
    }

    const requestId = adoptionRequestRef.current + 1;
    adoptionRequestRef.current = requestId;
    adoptionControllerRef.current?.abort();
    const controller = new AbortController();
    adoptionControllerRef.current = controller;
    adoptionActiveRef.current = true;
    setAdopting(true);
    setAdoptionError(null);
    setConflictMessage(null);

    try {
      const response = await api.adoptGuidelineRevision(
        boardId,
        guidelineId,
        {
          impact_receipt_id: currentReceipt.impact_receipt_id,
          impact_digest: currentReceipt.impact_digest,
          idempotency_key: adoptionIntentRef.current.idempotencyKey,
        },
        controller.signal,
      );
      if (
        controller.signal.aborted
        || requestId !== adoptionRequestRef.current
        || previewSignatureRef.current !== receiptSignature
      ) {
        return;
      }
      if (
        !isGuidelineAdoptionResponseForReceipt(response, currentReceipt)
      ) {
        throw new Error(
          'Guideline adoption returned a mismatched payload.',
        );
      }
      await onAdopted(response);
      onClose();
    } catch (error: unknown) {
      if (
        controller.signal.aborted
        || requestId !== adoptionRequestRef.current
      ) {
        return;
      }
      if (isGuidelineImpactConflict(error)) {
        setReceipt(null);
        setReceiptSignature('');
        setConflictMessage(
          'This impact receipt is no longer current. No board change was applied. Reload the impact and review it again.',
        );
      } else {
        setAdoptionError(guidelineImpactErrorMessage(error));
      }
    } finally {
      if (
        !controller.signal.aborted
        && requestId === adoptionRequestRef.current
      ) {
        adoptionActiveRef.current = false;
        setAdopting(false);
      }
    }
  }, [
    api,
    boardId,
    canAdopt,
    currentReceipt,
    guidelineId,
    onAdopted,
    onClose,
    previewLoading,
    receiptSignature,
  ]);

  const executableTargets = useMemo(
    () => Array.from(new Set(
      revisionAuthority?.revision.rules.flatMap(
        (rule) => rule.target_entity_types,
      ) ?? [],
    )),
    [revisionAuthority],
  );
  const containsBlockingRules = (
    revisionAuthority?.revision.rules.some(
      (rule) => rule.enforcement === 'blocking',
    ) ?? false
  );
  const updateAvailable = updateIsAvailable({
    adoptedBinding,
    targetRevisionId,
    targetSemanticVersion,
  });
  const impactCounts = currentReceipt
    ? countGuidelineImpactItems(currentReceipt.items)
    : null;
  const previewEnabled = (
    canPreview
    && priorityValid
    && activeRevisionState.status === 'ready'
    && !busy
  );
  const adoptEnabled = (
    canAdopt
    && currentReceipt !== null
    && !busy
  );

  let authorityMessage: string | null = null;
  if (permissions.isLoading) {
    authorityMessage = 'Checking policy permissions…';
  } else if (permissions.error) {
    authorityMessage =
      'Policy permissions are unavailable. Preview and adoption are disabled.';
  } else if (permissions.ownerReviewRequired) {
    authorityMessage =
      'Permission lineage requires owner review. Preview and adoption are disabled.';
  } else if (!canPreview) {
    authorityMessage =
      'You do not have permission to preview guideline impact.';
  } else if (activeRevisionState.status === 'loading') {
    authorityMessage = 'Loading exact revision authority…';
  } else if (activeRevisionState.status === 'error') {
    authorityMessage = activeRevisionState.error;
  }

  return (
    <div className="fixed inset-0 z-[70] flex items-center justify-center bg-black/55 p-4">
      <div
        ref={focusTrap.dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="guideline-impact-title"
        tabIndex={-1}
        onKeyDown={focusTrap.onKeyDown}
        className="flex max-h-[94vh] w-full max-w-5xl flex-col overflow-hidden rounded-xl border border-surface-200 bg-white shadow-2xl dark:border-surface-700 dark:bg-surface-900"
      >
        <header className="flex items-start justify-between gap-4 border-b border-surface-200 px-6 py-4 dark:border-surface-700">
          <div>
            <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-amber-600 dark:text-amber-300">
              Persisted impact preview
            </p>
            <h2
              id="guideline-impact-title"
              className="mt-1 text-xl font-semibold text-surface-900 dark:text-white"
            >
              Adopt {guidelineTitle} v{targetSemanticVersion}
            </h2>
            <p className="mt-1 text-sm text-surface-500 dark:text-surface-400">
              The current board binding does not change until explicit adoption succeeds.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <ContextualHelpLink
              sectionId="policy-governance"
              testId="guideline-impact-help"
            >
              Adoption guide
            </ContextualHelpLink>
            <button
              type="button"
              data-guideline-impact-initial-focus
              aria-label="Close impact preview"
              disabled={busy}
              onClick={onClose}
              className="rounded-lg p-2 text-surface-400 hover:bg-surface-100 hover:text-surface-700 disabled:opacity-40 dark:hover:bg-surface-800"
            >
              <X size={18} />
            </button>
          </div>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto p-6">
          <div className="grid gap-4 lg:grid-cols-[300px_minmax(0,1fr)]">
            <aside className="space-y-4">
              <div className="grid grid-cols-2 gap-2">
                <div className="rounded-lg border border-surface-200 p-3 dark:border-surface-700">
                  <div className="text-[10px] font-semibold uppercase text-surface-500">
                    Adopted
                  </div>
                  <div
                    className="mt-1 text-sm font-semibold text-surface-900 dark:text-white"
                    data-testid="guideline-impact-adopted-version"
                  >
                    {adoptedBinding
                      ? `v${adoptedBinding.semanticVersion}`
                      : 'Not adopted'}
                  </div>
                </div>
                <div className={`rounded-lg border p-3 ${
                  updateAvailable
                    ? 'border-amber-300 bg-amber-50 dark:border-amber-500/40 dark:bg-amber-500/10'
                    : 'border-surface-200 dark:border-surface-700'
                }`}>
                  <div className="text-[10px] font-semibold uppercase text-surface-500">
                    Latest
                  </div>
                  <div
                    className="mt-1 text-sm font-semibold text-surface-900 dark:text-white"
                    data-testid="guideline-impact-latest-version"
                  >
                    v{targetSemanticVersion}
                  </div>
                  {updateAvailable && (
                    <div
                      className="mt-0.5 text-[10px] font-medium text-amber-700 dark:text-amber-300"
                      data-testid="guideline-impact-update-available"
                    >
                      {adoptedBinding
                        ? 'Update available'
                        : 'Adoption available'}
                    </div>
                  )}
                </div>
              </div>

              <section className="rounded-lg border border-blue-200 bg-blue-50 p-3 dark:border-blue-500/30 dark:bg-blue-500/10">
                <div className="text-[10px] font-semibold uppercase text-blue-700 dark:text-blue-200">
                  Context scope
                </div>
                <div className="mt-1 text-sm font-semibold text-blue-950 dark:text-blue-100">
                  All entities
                </div>
                <p className="mt-1 text-xs text-blue-800/75 dark:text-blue-100/70">
                  Context availability is separate from executable targets and enforcement.
                </p>
              </section>

              <section className="rounded-lg border border-surface-200 p-3 dark:border-surface-700">
                <h3 className="text-xs font-semibold text-surface-800 dark:text-surface-100">
                  Executable targets
                </h3>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {executableTargets.length > 0 ? (
                    executableTargets.map((target) => (
                      <span
                        key={target}
                        className="rounded bg-surface-100 px-2 py-1 text-[10px] text-surface-700 dark:bg-surface-800 dark:text-surface-200"
                      >
                        {target}
                      </span>
                    ))
                  ) : (
                    <span className="text-xs text-surface-500">
                      Context only · no executable rules
                    </span>
                  )}
                </div>
                {containsBlockingRules && (
                  <div
                    className="mt-3 inline-flex items-center gap-1.5 rounded-full bg-red-50 px-2.5 py-1 text-xs font-medium text-red-700 dark:bg-red-500/15 dark:text-red-200"
                    data-testid="guideline-impact-contains-blocking"
                  >
                    <ShieldAlert size={13} aria-hidden="true" />
                    Contains blocking rules
                  </div>
                )}
              </section>

              <section className="rounded-lg border border-surface-200 p-3 dark:border-surface-700">
                <h3 className="text-xs font-semibold text-surface-800 dark:text-surface-100">
                  Proposed board binding
                </h3>
                <label className="mt-3 block text-xs font-medium text-surface-600 dark:text-surface-300">
                  Priority
                  <input
                    type="number"
                    min={0}
                    max={MAX_GUIDELINE_PRIORITY}
                    step={1}
                    value={priorityInput}
                    disabled={busy || !canPreview}
                    onChange={(event) => setPriorityInput(event.target.value)}
                    data-testid="guideline-impact-priority"
                    className="mt-1 w-full rounded-md border border-surface-300 bg-white px-3 py-2 text-sm text-surface-900 disabled:opacity-50 dark:border-surface-700 dark:bg-surface-950 dark:text-white"
                  />
                </label>
                {!priorityValid && (
                  <p role="alert" className="mt-1 text-xs text-red-600 dark:text-red-300">
                    Priority must be an integer from 0 to {MAX_GUIDELINE_PRIORITY}.
                  </p>
                )}
                <label className="mt-3 block text-xs font-medium text-surface-600 dark:text-surface-300">
                  Binding default enforcement
                  <select
                    value={enforcement}
                    disabled={busy || !canPreview}
                    onChange={(event) => setEnforcement(
                      event.target.value as GuidelineEnforcement,
                    )}
                    data-testid="guideline-impact-enforcement"
                    className="mt-1 w-full rounded-md border border-surface-300 bg-white px-3 py-2 text-sm text-surface-900 disabled:opacity-50 dark:border-surface-700 dark:bg-surface-950 dark:text-white"
                  >
                    <option value="advisory">Advisory</option>
                    <option value="blocking">Blocking</option>
                  </select>
                </label>
              </section>
            </aside>

            <main className="space-y-4">
              {authorityMessage && (
                <div
                  role="status"
                  className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-200"
                  data-testid="guideline-impact-authority-message"
                >
                  {authorityMessage}
                </div>
              )}

              <section className="rounded-xl border border-surface-200 p-4 dark:border-surface-700">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <h3 className="text-sm font-semibold text-surface-900 dark:text-white">
                      Impact evidence
                    </h3>
                    <p className="mt-1 text-xs text-surface-500 dark:text-surface-400">
                      Preview persists an immutable receipt but does not mutate the board.
                    </p>
                  </div>
                  <button
                    type="button"
                    disabled={!previewEnabled}
                    onClick={() => void runPreview(currentReceipt !== null)}
                    data-testid="guideline-impact-preview"
                    className="inline-flex min-h-9 items-center gap-1.5 rounded-lg border border-blue-300 bg-blue-50 px-3 py-1.5 text-xs font-semibold text-blue-700 hover:bg-blue-100 disabled:cursor-not-allowed disabled:opacity-40 dark:border-blue-700 dark:bg-blue-950/30 dark:text-blue-200"
                  >
                    {previewLoading ? (
                      <Loader2 size={14} className="animate-spin" aria-hidden="true" />
                    ) : (
                      <RefreshCw size={14} aria-hidden="true" />
                    )}
                    {previewLoading
                      ? 'Generating preview…'
                      : currentReceipt
                        ? 'Refresh impact'
                        : previewError
                          ? 'Try preview again'
                          : 'Preview impact'}
                  </button>
                </div>

                {previewError && (
                  <p
                    role="alert"
                    className="mt-3 rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-700 dark:border-red-800 dark:bg-red-950/20 dark:text-red-200"
                  >
                    {previewError}
                  </p>
                )}
                {conflictMessage && (
                  <div
                    role="alert"
                    className="mt-3 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-amber-300 bg-amber-50 p-3 text-xs text-amber-800 dark:border-amber-700 dark:bg-amber-950/20 dark:text-amber-200"
                    data-testid="guideline-impact-conflict"
                  >
                    <span className="flex min-w-0 items-start gap-2">
                      <AlertTriangle size={15} className="mt-0.5 shrink-0" aria-hidden="true" />
                      {conflictMessage}
                    </span>
                    <button
                      type="button"
                      disabled={!previewEnabled}
                      onClick={() => void runPreview(true)}
                      data-testid="guideline-impact-reload"
                      className="rounded-md border border-amber-400 px-2.5 py-1 font-semibold disabled:opacity-40"
                    >
                      Reload impact
                    </button>
                  </div>
                )}

                {!currentReceipt && !previewLoading && !previewError && !conflictMessage && (
                  <div
                    className="mt-4 flex items-start gap-2 rounded-lg border border-dashed border-surface-300 p-4 text-xs text-surface-500 dark:border-surface-700 dark:text-surface-400"
                    data-testid="guideline-impact-no-receipt"
                  >
                    <CircleOff size={16} className="mt-0.5 shrink-0" aria-hidden="true" />
                    Adopt stays disabled until a valid receipt is generated for these exact inputs.
                  </div>
                )}

                {currentReceipt && impactCounts && (
                  <div className="mt-4 space-y-4">
                    <div
                      className="flex items-start gap-2 rounded-lg border border-emerald-200 bg-emerald-50 p-3 text-xs text-emerald-800 dark:border-emerald-800 dark:bg-emerald-950/20 dark:text-emerald-200"
                      data-testid="guideline-impact-current-receipt"
                    >
                      <CheckCircle2 size={16} className="mt-0.5 shrink-0" aria-hidden="true" />
                      <span>
                        <strong>Current receipt for these inputs.</strong>{' '}
                        The server will re-check every fence atomically during adoption.
                      </span>
                    </div>

                    <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                      {([
                        ['Board bindings', impactCounts.binding],
                        ['Targets', impactCounts.target],
                        ['Artifacts', impactCounts.artifact],
                        ['Waivers', impactCounts.waiver],
                      ] as const).map(([label, value]) => (
                        <div
                          key={label}
                          className="rounded-lg bg-surface-50 p-3 dark:bg-surface-950/50"
                        >
                          <dt className="text-[10px] uppercase tracking-wide text-surface-500">
                            {label}
                          </dt>
                          <dd className="mt-1 text-xl font-semibold text-surface-900 dark:text-white">
                            {value}
                          </dd>
                        </div>
                      ))}
                    </dl>

                    {impactCounts.waiver > 0 && (
                      <div
                        className="flex items-start gap-2 rounded-lg border border-amber-300 bg-amber-50 p-3 text-xs text-amber-800 dark:border-amber-700 dark:bg-amber-950/20 dark:text-amber-200"
                        data-testid="guideline-impact-waiver-warning"
                      >
                        <FileWarning size={16} className="mt-0.5 shrink-0" aria-hidden="true" />
                        {impactCounts.waiver} governed waiver{impactCounts.waiver === 1 ? '' : 's'} require review or revalidation.
                      </div>
                    )}

                    <div className="grid gap-2 sm:grid-cols-3">
                      {([
                        ['Added rules', currentReceipt.added_rule_ids],
                        ['Changed rules', currentReceipt.changed_rule_ids],
                        ['Removed rules', currentReceipt.removed_rule_ids],
                      ] as const).map(([label, ids]) => (
                        <div
                          key={label}
                          className="rounded-lg border border-surface-200 p-3 dark:border-surface-700"
                        >
                          <div className="text-[10px] font-semibold uppercase text-surface-500">
                            {label} · {ids.length}
                          </div>
                          <p className="mt-1 break-all text-xs text-surface-700 dark:text-surface-200">
                            {ids.length > 0 ? ids.join(', ') : 'None'}
                          </p>
                        </div>
                      ))}
                    </div>

                    <div className="rounded-lg border border-surface-200 p-3 text-xs dark:border-surface-700">
                      <p className="text-surface-600 dark:text-surface-300">
                        Affected entity types:{' '}
                        {currentReceipt.affected_entity_types.length > 0
                          ? currentReceipt.affected_entity_types.join(', ')
                          : 'none'}
                      </p>
                      <p className="mt-2 text-surface-500 dark:text-surface-400">
                        Receipt {currentReceipt.impact_receipt_id} ·{' '}
                        {formatTimestamp(currentReceipt.created_at)}
                      </p>
                      <p className="mt-1 break-all font-mono text-[10px] text-surface-500 dark:text-surface-400">
                        Digest {currentReceipt.impact_digest}
                      </p>
                    </div>
                  </div>
                )}
              </section>

              {currentReceipt && (
                <CollapsibleEvidenceSection
                  title="Affected items"
                  description="Immutable detail projection, paginated with an opaque cursor."
                  expanded={itemsExpanded}
                  onToggle={() => setItemsExpanded((value) => !value)}
                  testId="guideline-impact-items"
                >
                  {impactItems.loaded && impactItems.items.length === 0 && !impactItems.loading ? (
                    <p className="rounded-lg border border-dashed border-surface-300 p-3 text-xs text-surface-500 dark:border-surface-700 dark:text-surface-400">
                      This receipt has no affected item.
                    </p>
                  ) : (
                    <ol className="space-y-2">
                      {impactItems.items.map((item) => (
                        <ImpactItemRow
                          key={item.impact_item_id}
                          item={item}
                        />
                      ))}
                    </ol>
                  )}
                  <CursorCollectionControls
                    collectionLabel="impact items"
                    itemCount={impactItems.items.length}
                    hasMore={impactItems.hasMore}
                    loading={impactItems.loading}
                    error={impactItems.error}
                    restartRequired={impactItems.restartRequired}
                    onLoadMore={impactItems.loadMore}
                    onRetry={impactItems.retry}
                    onRestart={impactItems.restart}
                    testId="guideline-impact-items-cursor"
                  />
                </CollapsibleEvidenceSection>
              )}
            </main>
          </div>
        </div>

        <footer className="flex flex-wrap items-center justify-between gap-3 border-t border-surface-200 px-6 py-4 dark:border-surface-700">
          <p className="text-xs text-surface-500 dark:text-surface-400">
            {canPreview && !canAdopt
              ? 'Preview access only. Adoption permission is required to mutate the board.'
              : currentReceipt
                ? 'Receipt current · explicit adoption is available.'
                : 'No current receipt · adoption is unavailable.'}
          </p>
          <div className="flex items-center gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={onClose}
              className="rounded-lg border border-surface-300 px-4 py-2 text-sm font-medium text-surface-700 disabled:opacity-40 dark:border-surface-700 dark:text-surface-200"
            >
              Cancel
            </button>
            <button
              type="button"
              disabled={!adoptEnabled}
              onClick={() => void adopt()}
              data-testid="guideline-impact-adopt"
              className="inline-flex min-h-10 items-center gap-1.5 rounded-lg bg-blue-600 px-4 py-2 text-sm font-semibold text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {adopting && <Loader2 size={15} className="animate-spin" aria-hidden="true" />}
              {adopting
                ? 'Adopting…'
                : `Adopt v${targetSemanticVersion}`}
            </button>
          </div>
          {adoptionError && (
            <p role="alert" className="w-full text-right text-xs text-red-600 dark:text-red-300">
              {adoptionError}
            </p>
          )}
        </footer>
      </div>
    </div>
  );
}

export default GuidelineImpactDialog;
