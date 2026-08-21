import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  CircleGauge,
  CircleOff,
  FileWarning,
  Gauge,
  Loader2,
  Plus,
  RefreshCw,
  ShieldCheck,
  ShieldOff,
  SlidersHorizontal,
  X,
} from 'lucide-react';

import { ContextualHelpLink } from '@/components/help';
import { CollapsibleEvidenceSection } from '@/components/shared/CollapsibleEvidenceSection';
import { useDialogFocusTrap } from '@/hooks/useDialogFocusTrap';
import { useEscapeToClose } from '@/hooks/useEscapeToClose';
import { usePermissions } from '@/hooks/usePermissions';
import { usePolicyGovernanceApi } from '@/services/policy-governance-api';
import type {
  GuidelineAdoptionResponse,
  GuidelineEnforcement,
  GuidelineImpactItem,
  GuidelineImpactPreviewResponse,
  GuidelineMetric,
  GuidelineMetricThresholdOverrides,
  GuidelineRevisionAuthorityResponse,
} from '@/types/policy-governance';

import {
  GUIDELINE_IMPACT_KIND_LABEL,
  countGuidelineImpactItems,
  createGuidelinePolicyClientId,
  guidelineImpactErrorMessage,
  isGuidelineAdoptionResponseForPreview,
  isGuidelineImpactConflict,
  isGuidelineImpactPreviewResponse,
  isGuidelineRevisionAuthorityForTarget,
} from './guidelineImpactModel';
import { isValidCustomMetricCode } from './semanticMetricEditorModel';

const DEFAULT_MINIMUM_CONFIDENCE = 70;

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
  proposedPriority: number;
  adoptedBinding?: AdoptedGuidelineBindingAuthority;
  initialEnforcement: GuidelineEnforcement;
  initialMinimumConfidence?: number;
  initialMetricThresholdOverrides?: GuidelineMetricThresholdOverrides;
  autoPreview?: boolean;
  onAddSemanticMetrics?: () => void;
  onClose: () => void;
  onAdopted: (
    response: GuidelineAdoptionResponse,
  ) => void | Promise<void>;
}

function canonicalOverrides(
  overrides: GuidelineMetricThresholdOverrides,
): GuidelineMetricThresholdOverrides {
  return Object.fromEntries(
    Object.entries(overrides).sort(([left], [right]) =>
      left.localeCompare(right)),
  );
}

function sameOverrides(
  left: GuidelineMetricThresholdOverrides,
  right: GuidelineMetricThresholdOverrides,
): boolean {
  return JSON.stringify(canonicalOverrides(left))
    === JSON.stringify(canonicalOverrides(right));
}

function parseScore(value: string): number | null {
  if (!/^\d+$/.test(value)) return null;
  const score = Number(value);
  return Number.isInteger(score) && score >= 0 && score <= 100
    ? score
    : null;
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
  if (!adoptedBinding) return true;
  return (
    adoptedBinding.revisionId !== targetRevisionId
    || adoptedBinding.semanticVersion !== targetSemanticVersion
  );
}

function ImpactItemRow({ item }: { item: GuidelineImpactItem }) {
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
        {item.entity_version !== null && (
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
    </li>
  );
}

function EnforcementOption({
  value,
  selected,
  disabled,
  onSelect,
}: {
  value: GuidelineEnforcement;
  selected: boolean;
  disabled: boolean;
  onSelect: () => void;
}) {
  const blocking = value === 'blocking';
  const Icon = blocking ? ShieldCheck : ShieldOff;
  return (
    <button
      type="button"
      aria-pressed={selected}
      disabled={disabled}
      onClick={onSelect}
      className={`flex min-h-24 items-start gap-3 rounded-xl border p-3 text-left transition ${
        selected
          ? blocking
            ? 'border-red-500 bg-red-50 ring-1 ring-red-500 dark:border-red-400 dark:bg-red-500/15'
            : 'border-blue-500 bg-blue-50 ring-1 ring-blue-500 dark:border-blue-400 dark:bg-blue-500/15'
          : 'border-surface-200 hover:border-blue-300 dark:border-surface-700 dark:hover:border-blue-600'
      } disabled:cursor-not-allowed disabled:opacity-40`}
    >
      <span className={`mt-0.5 flex h-10 w-10 shrink-0 items-center justify-center rounded-xl ${
        selected
          ? blocking
            ? 'bg-red-600 text-white'
            : 'bg-blue-600 text-white'
          : 'bg-surface-100 text-surface-500 dark:bg-surface-800'
      }`}>
        <Icon size={20} aria-hidden="true" />
      </span>
      <span>
        <span className="flex items-center gap-2 text-sm font-semibold text-surface-900 dark:text-white">
          {blocking ? 'Blocking' : 'Advisory'}
          {selected && <Check size={14} aria-hidden="true" />}
        </span>
        <span className="mt-1 block text-xs leading-relaxed text-surface-500 dark:text-surface-400">
          {blocking
            ? 'A failed current assessment can participate in supported transition gates.'
            : 'Records findings and recommendations without preventing transitions.'}
        </span>
      </span>
    </button>
  );
}

function MetricOverrideCard({
  metric,
  value,
  disabled,
  onChange,
}: {
  metric: GuidelineMetric;
  value: string | undefined;
  disabled: boolean;
  onChange: (next: string | undefined) => void;
}) {
  const overridden = value !== undefined;
  const effective = overridden ? value : String(metric.default_threshold);
  return (
    <article className="rounded-xl border border-surface-200 p-3 dark:border-surface-700">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h5 className="text-xs font-semibold text-surface-900 dark:text-white">
            {metric.title}
          </h5>
          <p className="mt-0.5 text-[11px] text-surface-500">
            {metric.direction === 'minimum'
              ? 'Higher is better · passes at or above'
              : 'Lower is better · passes at or below'}
          </p>
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={overridden}
          disabled={disabled}
          onClick={() => onChange(
            overridden ? undefined : String(metric.default_threshold),
          )}
          className={`rounded-full px-2.5 py-1 text-[10px] font-semibold ${
            overridden
              ? 'bg-violet-100 text-violet-700 dark:bg-violet-400/15 dark:text-violet-200'
              : 'bg-surface-100 text-surface-600 dark:bg-surface-800 dark:text-surface-300'
          } disabled:opacity-40`}
        >
          {overridden ? 'Board override' : 'Guideline default'}
        </button>
      </div>
      <div className="mt-3 flex items-center gap-3">
        <input
          type="range"
          min={0}
          max={100}
          step={1}
          aria-label={`${metric.title} threshold`}
          value={effective}
          disabled={disabled || !overridden}
          onChange={(event) => onChange(event.target.value)}
          className="min-w-0 flex-1 accent-violet-600 disabled:opacity-40"
        />
        <input
          type="number"
          min={0}
          max={100}
          step={1}
          aria-label={`${metric.title} threshold value`}
          value={effective}
          disabled={disabled || !overridden}
          onChange={(event) => onChange(event.target.value)}
          className="w-20 rounded-md border border-surface-300 bg-white px-2 py-1.5 text-center text-sm font-semibold text-surface-900 disabled:bg-surface-100 disabled:text-surface-500 dark:border-surface-700 dark:bg-surface-950 dark:text-white dark:disabled:bg-surface-800"
        />
      </div>
      <p className="mt-2 text-[10px] text-surface-500">
        Guideline default: {metric.default_threshold} · key{' '}
        <span className="font-mono">{metric.code}</span>
      </p>
    </article>
  );
}

export function GuidelineImpactDialog({
  boardId,
  guidelineId,
  guidelineTitle,
  targetRevisionId,
  targetSemanticVersion,
  proposedPriority,
  adoptedBinding,
  initialEnforcement,
  initialMinimumConfidence = DEFAULT_MINIMUM_CONFIDENCE,
  initialMetricThresholdOverrides = {},
  autoPreview = false,
  onAddSemanticMetrics,
  onClose,
  onAdopted,
}: GuidelineImpactDialogProps) {
  const api = usePolicyGovernanceApi();
  const permissions = usePermissions(boardId);
  const [enforcement, setEnforcement] =
    useState<GuidelineEnforcement>(initialEnforcement);
  const [minimumConfidenceInput, setMinimumConfidenceInput] = useState(
    String(initialMinimumConfidence),
  );
  const [overrideInputs, setOverrideInputs] = useState<Record<string, string>>(
    Object.fromEntries(
      Object.entries(initialMetricThresholdOverrides).map(
        ([code, threshold]) => [code, String(threshold)],
      ),
    ),
  );
  const [revisionState, setRevisionState] = useState<RevisionState>({
    scope: '',
    status: 'idle',
    authority: null,
    error: null,
  });
  const [preview, setPreview] =
    useState<GuidelineImpactPreviewResponse | null>(null);
  const [previewSignature, setPreviewSignature] = useState('');
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [conflictMessage, setConflictMessage] = useState<string | null>(null);
  const [adopting, setAdopting] = useState(false);
  const [adoptionError, setAdoptionError] = useState<string | null>(null);
  const [itemsExpanded, setItemsExpanded] = useState(true);
  const [technicalExpanded, setTechnicalExpanded] = useState(false);

  const previewControllerRef = useRef<AbortController | null>(null);
  const previewRequestRef = useRef(0);
  const previewActiveRef = useRef(false);
  const revisionControllerRef = useRef<AbortController | null>(null);
  const revisionRequestRef = useRef(0);
  const adoptionControllerRef = useRef<AbortController | null>(null);
  const adoptionRequestRef = useRef(0);
  const adoptionActiveRef = useRef(false);
  const adoptionIntentRef = useRef({ signature: '', idempotencyKey: '' });
  const previewIntentRef = useRef({ signature: '', idempotencyKey: '' });
  const autoPreviewAttemptRef = useRef('');

  const minimumConfidence = parseScore(minimumConfidenceInput);
  const parsedOverrides = useMemo(() => {
    const result: GuidelineMetricThresholdOverrides = {};
    const normalizedCodes = new Set<string>();
    for (const [code, input] of Object.entries(overrideInputs)) {
      const normalizedCode = code.toLowerCase();
      const score = parseScore(input);
      if (
        score === null
        || !isValidCustomMetricCode(code)
        || normalizedCodes.has(normalizedCode)
      ) {
        return null;
      }
      normalizedCodes.add(normalizedCode);
      result[code] = score;
    }
    return canonicalOverrides(result);
  }, [overrideInputs]);

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
        ) return;
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
        ) return;
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
  const metrics = useMemo(
    () => revisionAuthority?.revision.metrics ?? [],
    [revisionAuthority],
  );
  const metricCodes = useMemo(
    () => new Set(metrics.map((metric) => metric.code)),
    [metrics],
  );
  const unknownOverrideCodes = Object.keys(overrideInputs).filter(
    (code) => !metricCodes.has(code),
  );
  const configurationValid = (
    minimumConfidence !== null
    && parsedOverrides !== null
    && unknownOverrideCodes.length === 0
  );
  const contextOnly = revisionAuthority !== null && metrics.length === 0;
  const sameAdoptedRevision = Boolean(
    adoptedBinding
    && adoptedBinding.revisionId === targetRevisionId
    && adoptedBinding.semanticVersion === targetSemanticVersion,
  );
  const hasNoProposedChanges = Boolean(
    sameAdoptedRevision
    && configurationValid
    && enforcement === initialEnforcement
    && minimumConfidence === initialMinimumConfidence
    && parsedOverrides !== null
    && sameOverrides(parsedOverrides, initialMetricThresholdOverrides)
  );

  const currentSignature = JSON.stringify([
    boardId,
    guidelineId,
    targetRevisionId,
    targetSemanticVersion,
    proposedPriority,
    adoptedBinding?.bindingRevision ?? null,
    enforcement,
    minimumConfidence,
    parsedOverrides,
  ]);
  const currentSignatureRef = useRef(currentSignature);
  currentSignatureRef.current = currentSignature;

  useEffect(() => {
    previewRequestRef.current += 1;
    previewControllerRef.current?.abort();
    previewActiveRef.current = false;
    adoptionRequestRef.current += 1;
    adoptionControllerRef.current?.abort();
    adoptionActiveRef.current = false;
    setPreview(null);
    setPreviewSignature('');
    setPreviewLoading(false);
    setPreviewError(null);
    setConflictMessage(null);
    setAdopting(false);
    setAdoptionError(null);
    previewIntentRef.current = { signature: '', idempotencyKey: '' };
  }, [currentSignature]);

  useEffect(
    () => () => {
      previewControllerRef.current?.abort();
      revisionControllerRef.current?.abort();
      adoptionControllerRef.current?.abort();
    },
    [],
  );

  const currentPreview = (
    preview !== null
    && previewSignature === currentSignature
  )
    ? preview
    : null;

  const runPreview = useCallback(async () => {
    if (
      previewActiveRef.current
      || !canPreview
      || !configurationValid
      || parsedOverrides === null
      || minimumConfidence === null
      || revisionAuthority === null
      || hasNoProposedChanges
    ) {
      return;
    }
    const signature = currentSignature;
    const requestId = previewRequestRef.current + 1;
    previewRequestRef.current = requestId;
    previewControllerRef.current?.abort();
    const controller = new AbortController();
    previewControllerRef.current = controller;
    previewActiveRef.current = true;
    setPreviewLoading(true);
    setPreviewError(null);
    setConflictMessage(null);
    setAdoptionError(null);
    setPreview(null);
    setPreviewSignature('');

    if (
      previewIntentRef.current.signature !== signature
      || !previewIntentRef.current.idempotencyKey
    ) {
      previewIntentRef.current = {
        signature,
        idempotencyKey:
          createGuidelinePolicyClientId('guideline-impact-preview'),
      };
    }

    try {
      const response = await api.previewGuidelineImpact(
        boardId,
        guidelineId,
        {
          proposed_priority: proposedPriority,
          proposed_enforcement: enforcement,
          proposed_minimum_confidence: minimumConfidence,
          proposed_metric_threshold_overrides: parsedOverrides,
          idempotency_key: previewIntentRef.current.idempotencyKey,
          to_revision_id: targetRevisionId,
        },
        controller.signal,
      );
      if (
        controller.signal.aborted
        || requestId !== previewRequestRef.current
        || currentSignatureRef.current !== signature
      ) return;
      if (!isGuidelineImpactPreviewResponse(response, {
        boardId,
        guidelineId,
        targetRevisionId,
        targetSemanticVersion,
        targetRevisionDigest: revisionAuthority.revision.revision_digest,
        proposedPriority,
        proposedEnforcement: enforcement,
        proposedMinimumConfidence: minimumConfidence,
        proposedMetricThresholdOverrides: parsedOverrides,
        bindingId: adoptedBinding?.bindingId ?? null,
        bindingRevision: adoptedBinding?.bindingRevision ?? null,
        fromRevisionId: adoptedBinding?.revisionId ?? null,
        fromSemanticVersion: adoptedBinding?.semanticVersion ?? null,
        fromRevisionDigest: adoptedBinding?.revisionDigest ?? null,
      })) {
        throw new Error(
          'The impact preview returned a mismatched payload. Refresh the guideline and try again.',
        );
      }
      setPreview(response);
      setPreviewSignature(signature);
      setItemsExpanded(true);
      adoptionIntentRef.current = {
        signature: '',
        idempotencyKey: '',
      };
    } catch (error: unknown) {
      if (
        controller.signal.aborted
        || requestId !== previewRequestRef.current
        || currentSignatureRef.current !== signature
      ) return;
      if (isGuidelineImpactConflict(error)) {
        setConflictMessage(
          'The board guideline configuration changed while this preview was prepared. Reload it before applying changes.',
        );
      } else {
        setPreviewError(guidelineImpactErrorMessage(error));
      }
    } finally {
      if (
        !controller.signal.aborted
        && requestId === previewRequestRef.current
        && currentSignatureRef.current === signature
      ) {
        previewActiveRef.current = false;
        setPreviewLoading(false);
      }
    }
  }, [
    adoptedBinding,
    api,
    boardId,
    canPreview,
    configurationValid,
    currentSignature,
    enforcement,
    guidelineId,
    hasNoProposedChanges,
    minimumConfidence,
    parsedOverrides,
    proposedPriority,
    revisionAuthority,
    targetRevisionId,
    targetSemanticVersion,
  ]);

  useEffect(() => {
    if (
      !autoPreview
      || !canPreview
      || !configurationValid
      || revisionAuthority === null
      || hasNoProposedChanges
      || previewLoading
      || preview !== null
      || previewError !== null
      || conflictMessage !== null
      || autoPreviewAttemptRef.current === currentSignature
    ) return;
    autoPreviewAttemptRef.current = currentSignature;
    void runPreview();
  }, [
    autoPreview,
    canPreview,
    configurationValid,
    conflictMessage,
    currentSignature,
    hasNoProposedChanges,
    preview,
    previewError,
    previewLoading,
    revisionAuthority,
    runPreview,
  ]);

  const adopt = useCallback(async () => {
    if (
      adoptionActiveRef.current
      || !canAdopt
      || !currentPreview
      || previewLoading
    ) return;

    const signature = JSON.stringify([
      currentPreview.receipt.impact_receipt_id,
      currentPreview.receipt.impact_digest,
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
          impact_receipt_id: currentPreview.receipt.impact_receipt_id,
          impact_digest: currentPreview.receipt.impact_digest,
          idempotency_key: adoptionIntentRef.current.idempotencyKey,
        },
        controller.signal,
      );
      if (
        controller.signal.aborted
        || requestId !== adoptionRequestRef.current
        || currentSignatureRef.current !== previewSignature
      ) return;
      const expectedBindingRevision =
        (adoptedBinding?.bindingRevision ?? 0) + 1;
      if (
        !isGuidelineAdoptionResponseForPreview(
          response,
          currentPreview,
          expectedBindingRevision,
        )
      ) {
        throw new Error(
          'The board update response could not be verified. Refresh the board before trying again.',
        );
      }
      await onAdopted(response);
      onClose();
    } catch (error: unknown) {
      if (
        controller.signal.aborted
        || requestId !== adoptionRequestRef.current
      ) return;
      if (isGuidelineImpactConflict(error)) {
        setPreview(null);
        setPreviewSignature('');
        setConflictMessage(
          'This preview is no longer current. No board change was applied. Reload and review it again.',
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
    adoptedBinding?.bindingRevision,
    api,
    boardId,
    canAdopt,
    currentPreview,
    guidelineId,
    onAdopted,
    onClose,
    previewLoading,
    previewSignature,
  ]);

  const semanticTargets = useMemo(
    () => Array.from(new Set(
      metrics.flatMap((metric) => metric.target_entity_types),
    )),
    [metrics],
  );
  const updateAvailable = updateIsAvailable({
    adoptedBinding,
    targetRevisionId,
    targetSemanticVersion,
  });
  const impactCounts = currentPreview
    ? countGuidelineImpactItems(currentPreview.receipt.items)
    : null;
  const previewEnabled = (
    canPreview
    && configurationValid
    && activeRevisionState.status === 'ready'
    && !busy
    && !hasNoProposedChanges
  );
  const adoptEnabled = (
    canAdopt
    && currentPreview !== null
    && !busy
  );

  let authorityMessage: string | null = null;
  if (permissions.isLoading) {
    authorityMessage = 'Checking policy permissions…';
  } else if (permissions.error) {
    authorityMessage =
      'Policy permissions are unavailable. Preview and board changes are disabled.';
  } else if (permissions.ownerReviewRequired) {
    authorityMessage =
      'Permission lineage requires owner review. Preview and board changes are disabled.';
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
        className="flex max-h-[94vh] w-full max-w-6xl flex-col overflow-hidden rounded-xl border border-surface-200 bg-white shadow-2xl dark:border-surface-700 dark:bg-surface-900"
      >
        <header className="flex items-start justify-between gap-4 border-b border-surface-200 px-6 py-4 dark:border-surface-700">
          <div>
            <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-violet-600 dark:text-violet-300">
              Board guideline
            </p>
            <h2
              id="guideline-impact-title"
              className="mt-1 text-xl font-semibold text-surface-900 dark:text-white"
            >
              {adoptedBinding
                ? updateAvailable
                  ? `Review ${guidelineTitle} update`
                  : `Configure ${guidelineTitle}`
                : `Add ${guidelineTitle} to this board`}
            </h2>
            <p className="mt-1 text-sm text-surface-500 dark:text-surface-400">
              Choose how semantic assessments behave on this board, preview
              the impact, then confirm.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <ContextualHelpLink
              sectionId="semantic-guideline-metrics"
              testId="guideline-impact-help"
            >
              Semantic guideline guide
            </ContextualHelpLink>
            <button
              type="button"
              data-guideline-impact-initial-focus
              aria-label="Close guideline configuration"
              disabled={busy}
              onClick={onClose}
              className="rounded-lg p-2 text-surface-400 hover:bg-surface-100 hover:text-surface-700 disabled:opacity-40 dark:hover:bg-surface-800"
            >
              <X size={18} />
            </button>
          </div>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto p-6">
          <div className="grid gap-5 lg:grid-cols-[300px_minmax(0,1fr)]">
            <aside className="space-y-4">
              <div className="grid grid-cols-2 gap-2">
                <div className="rounded-lg border border-surface-200 p-3 dark:border-surface-700">
                  <div className="text-[10px] font-semibold uppercase text-surface-500">
                    On board
                  </div>
                  <div
                    className="mt-1 text-sm font-semibold text-surface-900 dark:text-white"
                    data-testid="guideline-impact-adopted-version"
                  >
                    {adoptedBinding
                      ? `v${adoptedBinding.semanticVersion}`
                      : 'Not added'}
                  </div>
                </div>
                <div className={`rounded-lg border p-3 ${
                  updateAvailable
                    ? 'border-violet-300 bg-violet-50 dark:border-violet-500/40 dark:bg-violet-500/10'
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
                      className="mt-0.5 text-[10px] font-medium text-violet-700 dark:text-violet-300"
                      data-testid="guideline-impact-update-available"
                    >
                      {adoptedBinding ? 'Update available' : 'Ready to add'}
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
                  The prose remains agent context whether or not custom metrics
                  are configured.
                </p>
              </section>

              <section className="rounded-lg border border-surface-200 p-3 dark:border-surface-700">
                <h3 className="flex items-center gap-2 text-xs font-semibold text-surface-800 dark:text-surface-100">
                  <Gauge size={15} className="text-violet-500" aria-hidden="true" />
                  Semantic assessment
                </h3>
                <p className="mt-2 text-2xl font-semibold text-surface-900 dark:text-white">
                  {metrics.length}
                  <span className="ml-1 text-xs font-normal text-surface-500">
                    custom metric{metrics.length === 1 ? '' : 's'}
                  </span>
                </p>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {semanticTargets.length > 0 ? (
                    semanticTargets.map((target) => (
                      <span
                        key={target}
                        className="rounded bg-surface-100 px-2 py-1 text-[10px] text-surface-700 dark:bg-surface-800 dark:text-surface-200"
                      >
                        {target}
                      </span>
                    ))
                  ) : (
                    <span className="text-xs text-surface-500">
                      Context only · no scored metric
                    </span>
                  )}
                </div>
                <div className="mt-3 flex items-start gap-2 rounded-lg bg-violet-50 p-2.5 text-[11px] text-violet-800 dark:bg-violet-500/10 dark:text-violet-200">
                  <CircleGauge size={15} className="mt-0.5 shrink-0" aria-hidden="true" />
                  Confidence is system-owned and never appears as a custom
                  metric or override.
                </div>
                {contextOnly && onAddSemanticMetrics && (
                  <button
                    type="button"
                    onClick={onAddSemanticMetrics}
                    data-testid="guideline-impact-add-metrics"
                    className="mt-3 inline-flex min-h-9 w-full items-center justify-center gap-1.5 rounded-lg border border-violet-300 bg-violet-50 px-3 py-1.5 text-xs font-semibold text-violet-700 hover:bg-violet-100 dark:border-violet-600 dark:bg-violet-500/10 dark:text-violet-200"
                  >
                    <Plus size={14} aria-hidden="true" />
                    Add semantic metrics
                  </button>
                )}
              </section>
            </aside>

            <main className="space-y-5">
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
                <div className="flex items-start gap-3">
                  <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-violet-100 text-violet-700 dark:bg-violet-500/15 dark:text-violet-200">
                    <SlidersHorizontal size={19} aria-hidden="true" />
                  </span>
                  <div>
                    <h3 className="text-sm font-semibold text-surface-900 dark:text-white">
                      Board behavior
                    </h3>
                    <p className="mt-1 text-xs text-surface-500 dark:text-surface-400">
                      These settings belong to this board binding. They do not
                      change the reusable guideline revision.
                    </p>
                  </div>
                </div>

                <fieldset className="mt-4">
                  <legend className="text-xs font-semibold text-surface-700 dark:text-surface-200">
                    Enforcement
                  </legend>
                  <div className="mt-2 grid gap-3 sm:grid-cols-2">
                    {(['advisory', 'blocking'] as const).map((value) => (
                      <EnforcementOption
                        key={value}
                        value={value}
                        selected={enforcement === value}
                        disabled={busy || !canPreview || contextOnly}
                        onSelect={() => setEnforcement(value)}
                      />
                    ))}
                  </div>
                  {contextOnly && (
                    <p className="mt-2 text-[11px] text-surface-500">
                      Context-only revisions do not evaluate metrics, so
                      enforcement becomes effective only after a revision adds
                      at least one custom metric.
                    </p>
                  )}
                </fieldset>

                <label className="mt-5 block text-xs font-semibold text-surface-700 dark:text-surface-200">
                  Minimum assessment confidence
                  <div className="mt-2 rounded-xl border border-surface-200 bg-surface-50 p-3 dark:border-surface-700 dark:bg-surface-950/50">
                    <div className="flex items-center gap-3">
                      <input
                        type="range"
                        min={0}
                        max={100}
                        step={1}
                        aria-label="Minimum assessment confidence"
                        value={minimumConfidenceInput}
                        disabled={busy || !canPreview || contextOnly}
                        onChange={(event) =>
                          setMinimumConfidenceInput(event.target.value)}
                        className="min-w-0 flex-1 accent-violet-600 disabled:opacity-40"
                      />
                      <input
                        type="number"
                        min={0}
                        max={100}
                        step={1}
                        aria-label="Minimum assessment confidence value"
                        value={minimumConfidenceInput}
                        disabled={busy || !canPreview || contextOnly}
                        onChange={(event) =>
                          setMinimumConfidenceInput(event.target.value)}
                        data-testid="guideline-impact-minimum-confidence"
                        className="w-20 rounded-md border border-surface-300 bg-white px-2 py-1.5 text-center text-sm font-semibold text-surface-900 dark:border-surface-700 dark:bg-surface-900 dark:text-white"
                      />
                    </div>
                    <p className="mt-2 text-[11px] font-normal text-surface-500">
                      Assessments below this confidence cannot satisfy a
                      blocking binding. Confidence is produced by the system,
                      not authored as a metric.
                    </p>
                  </div>
                </label>
                {minimumConfidence === null && (
                  <p role="alert" className="mt-1 text-xs text-red-600 dark:text-red-300">
                    Minimum confidence must be a whole number from 0 to 100.
                  </p>
                )}

                {metrics.length > 0 && (
                  <div className="mt-5">
                    <h4 className="text-xs font-semibold text-surface-700 dark:text-surface-200">
                      Metric thresholds
                    </h4>
                    <p className="mt-1 text-[11px] text-surface-500">
                      Keep each guideline default or enable a board-specific
                      override. Overrides are keyed by the metric’s stable key.
                    </p>
                    <div className="mt-3 grid gap-3 xl:grid-cols-2">
                      {metrics.map((metric) => (
                        <MetricOverrideCard
                          key={metric.metric_id}
                          metric={metric}
                          value={overrideInputs[metric.code]}
                          disabled={busy || !canPreview}
                          onChange={(next) => setOverrideInputs((current) => {
                            if (next === undefined) {
                              const updated = { ...current };
                              delete updated[metric.code];
                              return updated;
                            }
                            return { ...current, [metric.code]: next };
                          })}
                        />
                      ))}
                    </div>
                  </div>
                )}

                {unknownOverrideCodes.length > 0 && (
                  <div
                    role="alert"
                    className="mt-4 rounded-lg border border-amber-300 bg-amber-50 p-3 text-xs text-amber-800 dark:border-amber-700 dark:bg-amber-950/20 dark:text-amber-200"
                    data-testid="guideline-impact-orphan-overrides"
                  >
                    <p className="font-semibold">
                      This update removes metrics with board overrides.
                    </p>
                    <p className="mt-1">
                      Remove the stale overrides before previewing:{' '}
                      {unknownOverrideCodes.join(', ')}.
                    </p>
                    <div className="mt-2 flex flex-wrap gap-2">
                      {unknownOverrideCodes.map((code) => (
                        <button
                          key={code}
                          type="button"
                          disabled={busy}
                          onClick={() => setOverrideInputs((current) => {
                            const updated = { ...current };
                            delete updated[code];
                            return updated;
                          })}
                          className="rounded-md border border-amber-400 px-2 py-1 font-semibold"
                        >
                          Remove {code}
                        </button>
                      ))}
                    </div>
                  </div>
                )}
                {parsedOverrides === null && (
                  <p role="alert" className="mt-3 text-xs text-red-600 dark:text-red-300">
                    Every metric override must be a whole number from 0 to 100.
                  </p>
                )}
              </section>

              {hasNoProposedChanges && (
                <section
                  className="rounded-xl border border-blue-200 bg-blue-50 p-4 dark:border-blue-500/30 dark:bg-blue-500/10"
                  data-testid="guideline-impact-no-changes"
                >
                  <div className="flex items-start gap-3">
                    <CheckCircle2
                      size={19}
                      className="mt-0.5 shrink-0 text-blue-700 dark:text-blue-200"
                      aria-hidden="true"
                    />
                    <div>
                      <h3 className="text-sm font-semibold text-blue-950 dark:text-blue-100">
                        This guideline is already configured
                      </h3>
                      <p className="mt-1 text-xs text-blue-800/80 dark:text-blue-100/75">
                        This exact revision, enforcement, confidence threshold,
                        and metric overrides are active on the board.
                      </p>
                    </div>
                  </div>
                </section>
              )}

              <section className="rounded-xl border border-surface-200 p-4 dark:border-surface-700">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <h3 className="text-sm font-semibold text-surface-900 dark:text-white">
                      Impact preview
                    </h3>
                    <p className="mt-1 text-xs text-surface-500 dark:text-surface-400">
                      Evaluates this exact revision and board configuration
                      without applying a change.
                    </p>
                  </div>
                  <button
                    type="button"
                    disabled={!previewEnabled}
                    onClick={() => void runPreview()}
                    data-testid="guideline-impact-preview"
                    className="inline-flex min-h-9 items-center gap-1.5 rounded-lg border border-violet-300 bg-violet-50 px-3 py-1.5 text-xs font-semibold text-violet-700 hover:bg-violet-100 disabled:cursor-not-allowed disabled:opacity-40 dark:border-violet-700 dark:bg-violet-950/30 dark:text-violet-200"
                  >
                    {previewLoading ? (
                      <Loader2 size={14} className="animate-spin" aria-hidden="true" />
                    ) : (
                      <RefreshCw size={14} aria-hidden="true" />
                    )}
                    {previewLoading
                      ? 'Generating preview…'
                      : currentPreview
                        ? 'Refresh preview'
                        : previewError
                          ? 'Try again'
                          : 'Preview changes'}
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
                      onClick={() => void runPreview()}
                      data-testid="guideline-impact-reload"
                      className="rounded-md border border-amber-400 px-2.5 py-1 font-semibold disabled:opacity-40"
                    >
                      Reload impact
                    </button>
                  </div>
                )}

                {!currentPreview && !previewLoading && !previewError && !conflictMessage && (
                  <div
                    className="mt-4 flex items-start gap-2 rounded-lg border border-dashed border-surface-300 p-4 text-xs text-surface-500 dark:border-surface-700 dark:text-surface-400"
                    data-testid="guideline-impact-no-preview"
                  >
                    <CircleOff size={16} className="mt-0.5 shrink-0" aria-hidden="true" />
                    {hasNoProposedChanges
                      ? 'There are no board changes to preview.'
                      : autoPreview
                        ? 'Preparing a preview for these settings…'
                        : 'Preview these settings before applying them to the board.'}
                  </div>
                )}

                {currentPreview && impactCounts && (
                  <div className="mt-4 space-y-4">
                    <div
                      className="flex items-start gap-2 rounded-lg border border-emerald-200 bg-emerald-50 p-3 text-xs text-emerald-800 dark:border-emerald-800 dark:bg-emerald-950/20 dark:text-emerald-200"
                      data-testid="guideline-impact-current-preview"
                    >
                      <CheckCircle2 size={16} className="mt-0.5 shrink-0" aria-hidden="true" />
                      <span>
                        <strong>Impact preview is ready.</strong>{' '}
                        The server will verify its digest and binding head again
                        when you apply the change.
                      </span>
                    </div>

                    <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                      {([
                        ['Board configuration', impactCounts.binding],
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
                  </div>
                )}
              </section>

              {currentPreview && (
                <CollapsibleEvidenceSection
                  title="Affected items"
                  description="The immutable affected-item set sealed by this exact impact receipt."
                  expanded={itemsExpanded}
                  onToggle={() => setItemsExpanded((value) => !value)}
                  testId="guideline-impact-items"
                >
                  {currentPreview.receipt.items.length === 0 ? (
                    <p className="rounded-lg border border-dashed border-surface-300 p-3 text-xs text-surface-500 dark:border-surface-700 dark:text-surface-400">
                      This preview has no affected item.
                    </p>
                  ) : (
                    <ol className="space-y-2">
                      {currentPreview.receipt.items.map((item) => (
                        <ImpactItemRow
                          key={item.impact_item_id}
                          item={item}
                        />
                      ))}
                    </ol>
                  )}
                </CollapsibleEvidenceSection>
              )}

              {currentPreview && (
                <CollapsibleEvidenceSection
                  title="Technical details"
                  description="Immutable preview identity used for currentness and support."
                  expanded={technicalExpanded}
                  onToggle={() => setTechnicalExpanded((value) => !value)}
                  testId="guideline-impact-technical"
                >
                  <dl className="grid gap-3 rounded-lg border border-surface-200 p-3 text-xs dark:border-surface-700 sm:grid-cols-2">
                    <div>
                      <dt className="font-semibold text-surface-600 dark:text-surface-300">
                        Impact receipt ID
                      </dt>
                      <dd className="mt-1 break-all font-mono text-[10px] text-surface-500">
                        {currentPreview.receipt.impact_receipt_id}
                      </dd>
                    </div>
                    <div>
                      <dt className="font-semibold text-surface-600 dark:text-surface-300">
                        Impact digest
                      </dt>
                      <dd className="mt-1 break-all font-mono text-[10px] text-surface-500">
                        {currentPreview.receipt.impact_digest}
                      </dd>
                    </div>
                  </dl>
                </CollapsibleEvidenceSection>
              )}
            </main>
          </div>
        </div>

        <footer className="flex flex-wrap items-center justify-between gap-3 border-t border-surface-200 px-6 py-4 dark:border-surface-700">
          <p className="text-xs text-surface-500 dark:text-surface-400">
            {canPreview && !canAdopt
              ? 'Preview access only. Board configuration permission is required to apply changes.'
              : currentPreview
                ? 'Preview ready · confirm to apply this board configuration.'
                : hasNoProposedChanges
                  ? 'No changes to apply.'
                  : 'Review the impact before applying this board configuration.'}
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
              className="inline-flex min-h-10 items-center gap-1.5 rounded-lg bg-violet-600 px-4 py-2 text-sm font-semibold text-white hover:bg-violet-700 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {adopting && <Loader2 size={15} className="animate-spin" aria-hidden="true" />}
              {adopting
                ? 'Applying…'
                : adoptedBinding
                  ? updateAvailable
                    ? `Apply update to v${targetSemanticVersion}`
                    : 'Save board configuration'
                  : 'Add to board'}
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
