import {
  useEffect,
  useRef,
  useState,
} from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  ClipboardCheck,
  Info,
  MinusCircle,
  Plus,
  RefreshCw,
  Trash2,
} from 'lucide-react';
import toast from 'react-hot-toast';
import {
  useDashboardApi,
  type PageEnvelope,
} from '@/services/api';
import { AccessiblePaginator } from '@/components/shared/AccessiblePaginator';
import { CollapsibleEvidenceSection } from '@/components/shared/CollapsibleEvidenceSection';
import {
  PreviousResultsSection,
  TechnicalAuditSection,
  ValidationCycleHeader,
  ValidationCycleStatusBadge,
  type ValidationCycleState,
} from '@/components/validation-cycle/ValidationCyclePrimitives';
import type { PaginationPageSize } from '@/hooks/usePersistedPagination';
import { getErrorMessage } from '@/lib/getErrorMessage';
import type {
  CurrentQualityAssessment,
  IdeationStatus,
  QualityValidationCycleSummary,
  QualityAssessmentKind,
  QualityAssessmentListItem,
  QualityAssessmentReceiptState,
  QualityFinding,
  QualityFindingAnchorType,
  QualityFindingSeverity,
  QualitySubjectType,
  RefinementStatus,
  RecordAmbiguityAssessmentRequest,
  SpecStatus,
  ValidationCycleResultSummary,
  ValidationTechnicalAudit,
} from '@/types';
import { QualityGatePreviewCard } from './QualityGatePreview';

type VisibleQualityAssessmentKind = Exclude<
  QualityAssessmentKind,
  'spec_validation'
>;

const KIND_LABELS: Record<VisibleQualityAssessmentKind, string> = {
  ambiguity: 'Ambiguity',
  requirement_lint: 'Requirement lint',
};

const SEVERITIES: QualityFindingSeverity[] = [
  'info',
  'low',
  'medium',
  'high',
  'critical',
];

const ANCHOR_TYPES: QualityFindingAnchorType[] = [
  'whole_artifact',
  'field',
  'structured_child',
  'qa',
];

const CATEGORY_OPTIONS = [
  ['functional_scope_behavior', 'Functional scope and behavior'],
  ['domain_data_model', 'Domain and data model'],
  ['interaction_ux_flow', 'Interaction and UX flow'],
  ['nonfunctional_quality', 'Non-functional quality'],
  ['integration_dependencies', 'Integration and dependencies'],
  ['edge_failure_handling', 'Edge and failure handling'],
  ['constraints_tradeoffs', 'Constraints and trade-offs'],
  ['terminology_consistency', 'Terminology consistency'],
  ['acceptance_measurability', 'Acceptance measurability'],
  ['traceability_coverage', 'Traceability coverage'],
] as const;

const QUALITY_INTENT_CACHE_LIMIT = 24;

function emptyPage<T>(): PageEnvelope<T> {
  return {
    items: [],
    total_filtered: 0,
    total_overall: 0,
    offset: 0,
    limit: 25,
  };
}

function idempotencyKeyForFingerprint(
  cache: Map<string, string>,
  fingerprint: string,
): string {
  const existing = cache.get(fingerprint);
  if (existing) {
    // Refresh insertion order so eviction remains bounded and LRU-like.
    cache.delete(fingerprint);
    cache.set(fingerprint, existing);
    return existing;
  }
  const created = newClientKey('quality-ui');
  cache.set(fingerprint, created);
  while (cache.size > QUALITY_INTENT_CACHE_LIMIT) {
    const oldest = cache.keys().next().value;
    if (oldest === undefined) break;
    cache.delete(oldest);
  }
  return created;
}

function newClientKey(prefix: string): string {
  const uuid = globalThis.crypto?.randomUUID?.();
  if (uuid) return `${prefix}-${uuid}`;
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function formatTimestamp(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function severityTone(severity: QualityFindingSeverity): string {
  switch (severity) {
    case 'critical':
    case 'high':
      return 'bg-red-100 text-red-700 dark:bg-red-950/50 dark:text-red-300';
    case 'medium':
      return 'bg-amber-100 text-amber-700 dark:bg-amber-950/50 dark:text-amber-300';
    case 'low':
      return 'bg-blue-100 text-blue-700 dark:bg-blue-950/50 dark:text-blue-300';
    default:
      return 'bg-surface-100 text-surface-700 dark:bg-surface-800 dark:text-surface-300';
  }
}

function formatScore(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(1);
}

function currentReceiptTone(
  assessment: CurrentQualityAssessment,
  kind: VisibleQualityAssessmentKind,
): {
  card: string;
  ring: string;
  badge: string;
} {
  if (
    assessment.currentness !== 'current'
    || assessment.gate_preview.reason_code === 'ambiguity_gate_skipped'
  ) {
    return {
      card: 'border-amber-200 bg-amber-50/60 dark:border-amber-700/50 dark:bg-amber-950/20',
      ring: 'border-amber-400 text-amber-700 dark:border-amber-400 dark:text-amber-200',
      badge: 'bg-amber-100 text-amber-700 dark:bg-amber-400/15 dark:text-amber-200',
    };
  }
  if (kind === 'requirement_lint') {
    return {
      card: 'border-blue-200 bg-blue-50/40 dark:border-blue-800/60 dark:bg-blue-950/20',
      ring: 'border-blue-400 text-blue-700 dark:border-blue-400 dark:text-blue-200',
      badge: 'bg-blue-100 text-blue-700 dark:bg-blue-400/15 dark:text-blue-200',
    };
  }
  if (
    assessment.gate_preview.applicable
    && assessment.gate_preview.enabled
    && !assessment.gate_preview.allowed
  ) {
    return {
      card: 'border-red-200 bg-red-50/60 dark:border-red-700/50 dark:bg-red-950/20',
      ring: 'border-red-400 text-red-700 dark:border-red-400 dark:text-red-200',
      badge: 'bg-red-100 text-red-700 dark:bg-red-400/15 dark:text-red-200',
    };
  }
  if (
    assessment.gate_preview.applicable
    && assessment.gate_preview.enabled
    && assessment.gate_preview.reason_code === 'ambiguity_gate_ready'
  ) {
    return {
      card: 'border-emerald-200 bg-emerald-50/60 dark:border-emerald-700/50 dark:bg-emerald-950/20',
      ring: 'border-emerald-400 text-emerald-700 dark:border-emerald-400 dark:text-emerald-200',
      badge: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-400/15 dark:text-emerald-200',
    };
  }
  return {
    card: 'border-surface-200 bg-white dark:border-surface-700 dark:bg-surface-900/50',
    ring: 'border-blue-400 text-blue-700 dark:border-blue-400 dark:text-blue-200',
    badge: 'bg-blue-100 text-blue-700 dark:bg-blue-400/15 dark:text-blue-200',
  };
}

function currentReceiptHeadline(
  assessment: CurrentQualityAssessment,
  kind: VisibleQualityAssessmentKind,
): string {
  const label = KIND_LABELS[kind];
  if (assessment.currentness !== 'current') return `${label} assessment is a previous result`;
  if (kind === 'requirement_lint') return 'Requirement lint assessment';
  switch (assessment.gate_preview.reason_code) {
    case 'ambiguity_score_exceeds_threshold':
      return `${label} exceeds the allowed limit`;
    case 'ambiguity_gate_ready':
      return `${label} within the allowed limit`;
    case 'ambiguity_gate_skipped':
      return `${label} gate skipped by override`;
    case 'ambiguity_gate_disabled':
      return `${label} gate is disabled`;
    case 'ambiguity_assessment_stale':
      return `${label} assessment is a previous result`;
    default:
      return `${label} assessment`;
  }
}

function CurrentReceiptStatusIcon({
  assessment,
  kind,
}: {
  assessment: CurrentQualityAssessment;
  kind: VisibleQualityAssessmentKind;
}) {
  if (assessment.currentness !== 'current') {
    return (
      <AlertTriangle
        size={16}
        className="text-amber-600"
        aria-hidden="true"
        data-testid="quality-receipt-status-icon"
        data-state="stale"
      />
    );
  }
  if (kind === 'requirement_lint') {
    return (
      <Info
        size={16}
        className="text-blue-600"
        aria-hidden="true"
        data-testid="quality-receipt-status-icon"
        data-state="advisory"
      />
    );
  }
  if (assessment.gate_preview.reason_code === 'ambiguity_gate_skipped') {
    return (
      <AlertTriangle
        size={16}
        className="text-amber-600"
        aria-hidden="true"
        data-testid="quality-receipt-status-icon"
        data-state="skipped"
      />
    );
  }
  if (
    assessment.gate_preview.applicable
    && assessment.gate_preview.enabled
    && !assessment.gate_preview.allowed
  ) {
    return (
      <AlertTriangle
        size={16}
        className="text-red-600"
        aria-hidden="true"
        data-testid="quality-receipt-status-icon"
        data-state="blocked"
      />
    );
  }
  if (assessment.gate_preview.reason_code === 'ambiguity_gate_ready') {
    return (
      <CheckCircle2
        size={16}
        className="text-emerald-600"
        aria-hidden="true"
        data-testid="quality-receipt-status-icon"
        data-state="ready"
      />
    );
  }
  return (
    <MinusCircle
      size={16}
      className="text-surface-500"
      aria-hidden="true"
      data-testid="quality-receipt-status-icon"
      data-state="neutral"
    />
  );
}

function QualityScoreRing({
  assessment,
  kind,
}: {
  assessment: CurrentQualityAssessment;
  kind: VisibleQualityAssessmentKind;
}) {
  const tone = currentReceiptTone(assessment, kind);
  const score = formatScore(assessment.receipt.score);
  const maximum = formatScore(assessment.receipt.scale.maximum);
  const accessibleLabel = `${KIND_LABELS[kind]} score ${score} out of ${maximum}`;

  return (
    <div
      role="img"
      aria-label={accessibleLabel}
      data-testid="quality-score-ring"
      className={`flex h-16 w-16 shrink-0 items-center justify-center rounded-full border-4 ${tone.ring}`}
    >
      <span aria-hidden="true" className="text-2xl font-bold leading-none">
        {score}
        <span className="ml-0.5 text-sm font-semibold text-surface-500 dark:text-surface-400">
          /{maximum}
        </span>
      </span>
    </div>
  );
}

interface FindingDraft {
  key: string;
  categoryCode: string;
  severity: QualityFindingSeverity;
  title: string;
  detail: string;
  anchorType: QualityFindingAnchorType;
  anchorRef: string;
  remediation: string;
}

interface QuestionDraft {
  key: string;
  question: string;
  findingKey: string;
}

function createFindingDraft(): FindingDraft {
  return {
    key: newClientKey('finding'),
    categoryCode: CATEGORY_OPTIONS[0][0],
    severity: 'medium',
    title: '',
    detail: '',
    anchorType: 'whole_artifact',
    anchorRef: '',
    remediation: '',
  };
}

function createQuestionDraft(): QuestionDraft {
  return {
    key: newClientKey('question'),
    question: '',
    findingKey: '',
  };
}

function ManualAssessmentForm({
  subjectType,
  subjectId,
  subjectVersion,
  subjectEdition,
  expectedHeadRevision,
  canProposeQuestions,
  disabled,
  onRecorded,
}: {
  subjectType: 'ideation' | 'refinement';
  subjectId: string;
  subjectVersion: number;
  subjectEdition: number;
  expectedHeadRevision: number;
  canProposeQuestions: boolean;
  disabled: boolean;
  onRecorded: () => void;
}) {
  const api = useDashboardApi();
  const [expanded, setExpanded] = useState(false);
  const [score, setScore] = useState(1);
  const [findings, setFindings] = useState<FindingDraft[]>([]);
  const [questions, setQuestions] = useState<QuestionDraft[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [validationError, setValidationError] = useState<string | null>(null);
  const idempotencyKeysByFingerprintRef = useRef(new Map<string, string>());

  useEffect(() => {
    idempotencyKeysByFingerprintRef.current.clear();
  }, [subjectId, subjectType]);

  useEffect(() => {
    if (!canProposeQuestions) setQuestions([]);
  }, [canProposeQuestions]);

  const updateFinding = (
    key: string,
    patch: Partial<FindingDraft>,
  ) => {
    setFindings((items) => items.map((item) => (
      item.key === key ? { ...item, ...patch } : item
    )));
  };

  const updateQuestion = (
    key: string,
    patch: Partial<QuestionDraft>,
  ) => {
    setQuestions((items) => items.map((item) => (
      item.key === key ? { ...item, ...patch } : item
    )));
  };

  const removeFinding = (findingKey: string) => {
    setFindings((items) => items.filter((item) => item.key !== findingKey));
    setQuestions((items) => items.map((item) => (
      item.findingKey === findingKey
        ? { ...item, findingKey: '' }
        : item
    )));
  };

  const validate = (): string | null => {
    if (score > 1 && findings.length === 0) {
      return 'Scores above 1 require at least one pinpoint finding.';
    }
    for (const finding of findings) {
      if (!finding.title.trim() || !finding.detail.trim()) {
        return 'Every finding needs a title and detail.';
      }
      if (finding.anchorType !== 'whole_artifact' && !finding.anchorRef.trim()) {
        return 'Field, structured-child and Q&A anchors require an anchor reference.';
      }
    }
    if (
      canProposeQuestions
      && questions.some((question) => !question.question.trim())
    ) {
      return 'Remove empty proposed questions or enter their text.';
    }
    return null;
  };

  const submit = async () => {
    const invalid = validate();
    setValidationError(invalid);
    if (invalid) return;

    const intent: Omit<RecordAmbiguityAssessmentRequest, 'idempotency_key'> = {
      expected_subject_version: subjectVersion,
      expected_subject_edition: subjectEdition,
      expected_head_revision: expectedHeadRevision,
      score,
      findings: findings.map((finding) => ({
        finding_key: finding.key,
        category_code: finding.categoryCode,
        severity: finding.severity,
        confidence: 1,
        deterministic: false,
        title: finding.title.trim(),
        detail: finding.detail.trim(),
        anchor: {
          anchor_type: finding.anchorType,
          anchor_ref: finding.anchorType === 'whole_artifact'
            ? null
            : finding.anchorRef.trim(),
          excerpt_hash: null,
        },
        evidence_refs: [],
        remediation: finding.remediation.trim() || null,
        rule_code: null,
      })),
      proposed_questions: canProposeQuestions
        ? questions.map((question) => ({
            client_key: question.key,
            question: question.question.trim(),
            question_type: 'text',
            choices: [],
            allow_free_text: true,
            category_code: null,
            finding_keys: question.findingKey ? [question.findingKey] : [],
          }))
        : [],
    };
    const fingerprint = JSON.stringify({
      subject_type: subjectType,
      subject_id: subjectId,
      ...intent,
    });
    const payload: RecordAmbiguityAssessmentRequest = {
      idempotency_key: idempotencyKeyForFingerprint(
        idempotencyKeysByFingerprintRef.current,
        fingerprint,
      ),
      ...intent,
    };

    setSubmitting(true);
    setValidationError(null);
    try {
      const result = await api.recordAmbiguityAssessment(
        subjectType,
        subjectId,
        payload,
      );
      toast.success(result.replayed
        ? 'Ambiguity assessment replayed'
        : 'Ambiguity assessment recorded');
      // A committed write ends the retry session. Even an older failed intent
      // must receive a fresh key if the user deliberately submits it later.
      idempotencyKeysByFingerprintRef.current.clear();
      setScore(1);
      setFindings([]);
      setQuestions([]);
      setExpanded(false);
      onRecorded();
    } catch (reason) {
      setValidationError(getErrorMessage(reason));
      toast.error(getErrorMessage(reason));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <section
      className="rounded-xl border border-blue-200 bg-blue-50/50 p-4 dark:border-blue-800/50 dark:bg-blue-950/20"
      data-testid="quality-manual-assessment"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold text-blue-900 dark:text-blue-100">
              New assessment
          </h3>
          <p className="mt-0.5 text-xs text-blue-700 dark:text-blue-300">
            Records a new result for Edition {subjectEdition}.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setExpanded((value) => !value)}
          disabled={disabled || submitting}
          className="inline-flex items-center gap-1 rounded-lg border border-blue-300 bg-white px-3 py-1.5 text-xs font-medium text-blue-700 hover:bg-blue-100 disabled:cursor-not-allowed disabled:opacity-50 dark:border-blue-700 dark:bg-blue-950/30 dark:text-blue-200"
          aria-expanded={expanded}
        >
          {expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
          {expanded ? 'Close form' : 'Record assessment'}
        </button>
      </div>

      {expanded && (
        <div className="mt-4 space-y-4">
          <label className="block text-xs font-medium text-surface-700 dark:text-surface-200">
            Ambiguity score
            <select
              value={score}
              onChange={(event) => setScore(Number(event.target.value))}
              disabled={submitting}
              className="mt-1 block min-h-9 w-full rounded-lg border border-surface-300 bg-white px-2 py-1.5 text-sm dark:border-surface-600 dark:bg-surface-900"
              aria-label="Ambiguity score"
            >
              {[1, 2, 3, 4, 5].map((value) => (
                <option key={value} value={value}>
                  {value} — {value === 1 ? 'clear' : value === 5 ? 'highly ambiguous' : 'ambiguity present'}
                </option>
              ))}
            </select>
          </label>

          <div className="space-y-3">
            <div className="flex items-center justify-between gap-2">
              <div>
                <h4 className="text-xs font-semibold text-surface-800 dark:text-surface-100">
                  Pinpoint findings
                </h4>
                <p className="text-[11px] text-surface-500 dark:text-surface-400">
                  Required when the score is above 1.
                </p>
              </div>
              <button
                type="button"
                onClick={() => setFindings((items) => [...items, createFindingDraft()])}
                disabled={submitting}
                className="inline-flex items-center gap-1 rounded-lg border border-surface-300 bg-white px-2.5 py-1.5 text-xs font-medium text-surface-700 hover:bg-surface-100 disabled:opacity-50 dark:border-surface-600 dark:bg-surface-900 dark:text-surface-200"
              >
                <Plus size={13} /> Add finding
              </button>
            </div>

            {findings.map((finding, index) => (
              <fieldset
                key={finding.key}
                className="space-y-3 rounded-lg border border-surface-200 bg-white p-3 dark:border-surface-700 dark:bg-surface-900/60"
              >
                <legend className="px-1 text-xs font-semibold text-surface-700 dark:text-surface-200">
                  Finding {index + 1}
                </legend>
                <div className="grid gap-3 sm:grid-cols-2">
                  <label className="text-xs font-medium text-surface-700 dark:text-surface-200">
                    Category
                    <select
                      value={finding.categoryCode}
                      onChange={(event) => updateFinding(finding.key, { categoryCode: event.target.value })}
                      className="mt-1 block min-h-9 w-full rounded-lg border border-surface-300 bg-white px-2 py-1.5 text-xs dark:border-surface-600 dark:bg-surface-800"
                    >
                      {CATEGORY_OPTIONS.map(([value, label]) => (
                        <option key={value} value={value}>{label}</option>
                      ))}
                    </select>
                  </label>
                  <label className="text-xs font-medium text-surface-700 dark:text-surface-200">
                    Severity
                    <select
                      value={finding.severity}
                      onChange={(event) => updateFinding(finding.key, {
                        severity: event.target.value as QualityFindingSeverity,
                      })}
                      className="mt-1 block min-h-9 w-full rounded-lg border border-surface-300 bg-white px-2 py-1.5 text-xs dark:border-surface-600 dark:bg-surface-800"
                    >
                      {SEVERITIES.map((severity) => (
                        <option key={severity} value={severity}>{severity}</option>
                      ))}
                    </select>
                  </label>
                </div>
                <label className="block text-xs font-medium text-surface-700 dark:text-surface-200">
                  Title
                  <input
                    value={finding.title}
                    onChange={(event) => updateFinding(finding.key, { title: event.target.value })}
                    maxLength={240}
                    className="mt-1 block min-h-9 w-full rounded-lg border border-surface-300 bg-white px-2.5 py-1.5 text-xs dark:border-surface-600 dark:bg-surface-800"
                    placeholder="Concise ambiguity"
                  />
                </label>
                <label className="block text-xs font-medium text-surface-700 dark:text-surface-200">
                  Detail
                  <textarea
                    value={finding.detail}
                    onChange={(event) => updateFinding(finding.key, { detail: event.target.value })}
                    rows={3}
                    className="mt-1 block w-full resize-y rounded-lg border border-surface-300 bg-white px-2.5 py-2 text-xs dark:border-surface-600 dark:bg-surface-800"
                    placeholder="Explain what is ambiguous and why it matters"
                  />
                </label>
                <div className="grid gap-3 sm:grid-cols-2">
                  <label className="text-xs font-medium text-surface-700 dark:text-surface-200">
                    Anchor
                    <select
                      value={finding.anchorType}
                      onChange={(event) => updateFinding(finding.key, {
                        anchorType: event.target.value as QualityFindingAnchorType,
                      })}
                      className="mt-1 block min-h-9 w-full rounded-lg border border-surface-300 bg-white px-2 py-1.5 text-xs dark:border-surface-600 dark:bg-surface-800"
                    >
                      {ANCHOR_TYPES.map((anchor) => (
                        <option key={anchor} value={anchor}>{anchor.split('_').join(' ')}</option>
                      ))}
                    </select>
                  </label>
                  <label className="text-xs font-medium text-surface-700 dark:text-surface-200">
                    Anchor reference
                    <input
                      value={finding.anchorRef}
                      onChange={(event) => updateFinding(finding.key, { anchorRef: event.target.value })}
                      disabled={finding.anchorType === 'whole_artifact'}
                      className="mt-1 block min-h-9 w-full rounded-lg border border-surface-300 bg-white px-2.5 py-1.5 text-xs disabled:bg-surface-100 disabled:opacity-60 dark:border-surface-600 dark:bg-surface-800"
                      placeholder={finding.anchorType === 'field' ? 'e.g. problem_statement' : 'Entity or Q&A reference'}
                    />
                  </label>
                </div>
                <label className="block text-xs font-medium text-surface-700 dark:text-surface-200">
                  Suggested remediation (optional)
                  <input
                    value={finding.remediation}
                    onChange={(event) => updateFinding(finding.key, { remediation: event.target.value })}
                    className="mt-1 block min-h-9 w-full rounded-lg border border-surface-300 bg-white px-2.5 py-1.5 text-xs dark:border-surface-600 dark:bg-surface-800"
                  />
                </label>
                <div className="flex justify-end">
                  <button
                    type="button"
                    onClick={() => removeFinding(finding.key)}
                    className="inline-flex items-center gap-1 text-xs text-red-600 hover:text-red-700 dark:text-red-400"
                  >
                    <Trash2 size={13} /> Remove finding
                  </button>
                </div>
              </fieldset>
            ))}
          </div>

          {canProposeQuestions && <div className="space-y-3">
            <div className="flex items-center justify-between gap-2">
              <div>
                <h4 className="text-xs font-semibold text-surface-800 dark:text-surface-100">
                  Proposed clarification questions
                </h4>
                <p className="text-[11px] text-surface-500 dark:text-surface-400">
                  Optional; attach up to five clarification suggestions to this assessment. They do not create Q&amp;A items.
                </p>
              </div>
              <button
                type="button"
                onClick={() => setQuestions((items) => [...items, createQuestionDraft()])}
                disabled={submitting || questions.length >= 5}
                className="inline-flex items-center gap-1 rounded-lg border border-surface-300 bg-white px-2.5 py-1.5 text-xs font-medium text-surface-700 hover:bg-surface-100 disabled:opacity-50 dark:border-surface-600 dark:bg-surface-900 dark:text-surface-200"
              >
                <Plus size={13} /> Add question
              </button>
            </div>
            {questions.map((question, index) => (
              <div
                key={question.key}
                className="grid gap-2 rounded-lg border border-surface-200 bg-white p-3 dark:border-surface-700 dark:bg-surface-900/60 sm:grid-cols-[1fr_12rem_auto]"
              >
                <label className="text-xs font-medium text-surface-700 dark:text-surface-200">
                  Question {index + 1}
                  <input
                    value={question.question}
                    onChange={(event) => updateQuestion(question.key, { question: event.target.value })}
                    className="mt-1 block min-h-9 w-full rounded-lg border border-surface-300 bg-white px-2.5 py-1.5 text-xs dark:border-surface-600 dark:bg-surface-800"
                  />
                </label>
                <label className="text-xs font-medium text-surface-700 dark:text-surface-200">
                  Linked finding
                  <select
                    value={question.findingKey}
                    onChange={(event) => updateQuestion(question.key, { findingKey: event.target.value })}
                    className="mt-1 block min-h-9 w-full rounded-lg border border-surface-300 bg-white px-2 py-1.5 text-xs dark:border-surface-600 dark:bg-surface-800"
                  >
                    <option value="">None</option>
                    {findings.map((finding, findingIndex) => (
                      <option key={finding.key} value={finding.key}>
                        Finding {findingIndex + 1}
                      </option>
                    ))}
                  </select>
                </label>
                <button
                  type="button"
                  onClick={() => setQuestions((items) => items.filter((item) => item.key !== question.key))}
                  className="self-end rounded-lg p-2 text-red-600 hover:bg-red-50 dark:text-red-400 dark:hover:bg-red-950/30"
                  aria-label={`Remove question ${index + 1}`}
                >
                  <Trash2 size={15} />
                </button>
              </div>
            ))}
          </div>}

          {validationError && (
            <p
              role="alert"
              className="rounded-lg border border-red-200 bg-red-50 p-2.5 text-xs text-red-700 dark:border-red-800 dark:bg-red-950/30 dark:text-red-300"
            >
              {validationError}
            </p>
          )}

          <div className="flex justify-end">
            <button
              type="button"
              onClick={() => void submit()}
              disabled={disabled || submitting}
              className="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 px-4 py-2 text-xs font-semibold text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <ClipboardCheck size={15} />
              {submitting ? 'Recording…' : 'Record governed assessment'}
            </button>
          </div>
        </div>
      )}
    </section>
  );
}

function HistoryItems({ page }: { page: PageEnvelope<QualityAssessmentListItem> }) {
  if (page.items.length === 0) return null;
  return (
    <ol className="space-y-2" data-testid="quality-assessment-history">
      {page.items.map((item) => (
        <li
          key={item.receipt.id}
          className="rounded-lg border border-surface-200 bg-white p-3 dark:border-surface-700 dark:bg-surface-900/50"
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex flex-wrap items-center gap-2">
              <strong className="text-sm text-surface-800 dark:text-surface-100">
                {item.receipt.score}
              </strong>
              <span className="rounded-full bg-surface-100 px-2 py-0.5 text-[10px] font-medium text-surface-600 dark:bg-surface-800 dark:text-surface-300">
                {item.state}
              </span>
              {item.is_head && (
                <span className="rounded-full bg-blue-100 px-2 py-0.5 text-[10px] font-medium text-blue-700 dark:bg-blue-950/50 dark:text-blue-300">
                  head
                </span>
              )}
            </div>
            <time className="text-[11px] text-surface-500 dark:text-surface-400">
              {formatTimestamp(item.receipt.created_at)}
            </time>
          </div>
          <p className="mt-1 break-all text-[11px] text-surface-500 dark:text-surface-400">
            Receipt {item.receipt.id} · subject v{item.receipt.subject_version}
          </p>
          {item.currentness.stale_reasons.length > 0 && (
            <p className="mt-1 text-xs text-amber-700 dark:text-amber-300">
              {item.currentness.stale_reasons.map((reason) => reason.split('_').join(' ')).join(', ')}
            </p>
          )}
        </li>
      ))}
    </ol>
  );
}

function FindingItems({
  page,
  anchorTexts,
  showTechnicalMetadata = true,
}: {
  page: PageEnvelope<QualityFinding>;
  anchorTexts?: Record<string, string>;
  showTechnicalMetadata?: boolean;
}) {
  if (page.items.length === 0) return null;
  return (
    <ol className="space-y-2" data-testid="quality-findings">
      {page.items.map((finding) => (
        <li
          key={finding.id}
          className="rounded-lg border border-surface-200 bg-white p-3 dark:border-surface-700 dark:bg-surface-900/50"
        >
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-1.5">
                <span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${severityTone(finding.severity)}`}>
                  {finding.severity}
                </span>
                <span className="rounded-full bg-violet-100 px-2 py-0.5 text-[10px] font-medium text-violet-700 dark:bg-violet-950/50 dark:text-violet-300">
                  {finding.category_code.split('_').join(' ')}
                </span>
                <span className="rounded-full bg-surface-100 px-2 py-0.5 text-[10px] text-surface-600 dark:bg-surface-800 dark:text-surface-300">
                  {finding.lifecycle}
                </span>
              </div>
              <h4 className="mt-2 text-sm font-semibold text-surface-800 dark:text-surface-100">
                {finding.title}
              </h4>
            </div>
            {finding.blocking_eligible && (
              <span className="inline-flex items-center gap-1 text-[10px] font-medium text-red-600 dark:text-red-300">
                <AlertTriangle size={12} /> blocking eligible
              </span>
            )}
          </div>
          <p className="mt-1 whitespace-pre-wrap text-xs text-surface-700 dark:text-surface-300">
            {finding.detail}
          </p>
          {finding.anchor.anchor_ref
            && anchorTexts?.[finding.anchor.anchor_ref] && (
            <blockquote
              data-testid="quality-finding-requirement"
              className="mt-2 border-l-2 border-violet-300 bg-surface-50 py-1 pl-2 pr-1 text-xs text-surface-800 dark:border-violet-700 dark:bg-surface-900/60 dark:text-surface-100"
            >
              {anchorTexts[finding.anchor.anchor_ref]}
            </blockquote>
          )}
          {showTechnicalMetadata && (
            <p className="mt-2 text-[11px] text-surface-500 dark:text-surface-400">
              Anchor: {finding.anchor.anchor_type.split('_').join(' ')}
              {finding.anchor.anchor_ref ? ` · ${finding.anchor.anchor_ref}` : ''}
              {' '}· subject v{finding.anchor.subject_version}
            </p>
          )}
          {finding.remediation && (
            <p className="mt-1 text-xs text-emerald-700 dark:text-emerald-300">
              Suggested remediation: {finding.remediation}
            </p>
          )}
        </li>
      ))}
    </ol>
  );
}

function lifecycleQualityState(
  assessment: CurrentQualityAssessment | null,
  kind: VisibleQualityAssessmentKind,
): ValidationCycleState {
  if (!assessment || assessment.currentness !== 'current') return 'not_started';
  if (kind === 'requirement_lint') {
    return assessment.receipt.score === 0 ? 'passed' : 'needs_attention';
  }
  if (
    assessment.gate_preview.applicable
    && assessment.gate_preview.enabled
    && !assessment.gate_preview.allowed
  ) {
    return 'failed';
  }
  return 'passed';
}

function summaryValue(
  result: ValidationCycleResultSummary | null,
  key: string,
): unknown {
  return result?.summary[key];
}

function summaryNumber(
  result: ValidationCycleResultSummary | null,
  key: string,
): number | null {
  const value = summaryValue(result, key);
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function summaryText(
  result: ValidationCycleResultSummary | null,
  key: string,
): string | null {
  const value = summaryValue(result, key);
  return typeof value === 'string' && value.trim() ? value : null;
}

function lifecycleSummaryState(
  result: ValidationCycleResultSummary | null,
): ValidationCycleState {
  if (!result) return 'not_started';
  switch (result.status.trim().toLowerCase()) {
    case 'success':
    case 'pass':
    case 'passed':
    case 'approved':
      return 'passed';
    case 'failed':
    case 'fail':
    case 'rejected':
      return 'failed';
    case 'pending':
    case 'running':
    case 'in_progress':
      return 'in_progress';
    case 'blocked':
    case 'warning':
    case 'needs_attention':
      return 'needs_attention';
    default:
      return 'completed';
  }
}

function LifecyclePreviousQualityResults({
  results,
  currentReceiptId,
}: {
  results: ValidationCycleResultSummary[];
  currentReceiptId?: string;
}) {
  const previous = results.filter(
    (item) => item.result_id !== currentReceiptId,
  );
  if (previous.length === 0) {
    return (
      <p className="text-xs text-surface-500 dark:text-surface-400">
        No previous results are available.
      </p>
    );
  }
  return (
    <ol className="space-y-2" data-testid="quality-previous-results">
      {previous.map((item) => {
        const edition = item.subject_edition;
        const state = lifecycleSummaryState(item);
        const score = summaryNumber(item, 'score');
        const scaleMaximum = summaryNumber(item, 'scale_maximum') ?? 5;
        const createdAt = summaryText(item, 'created_at')
          ?? summaryText(item, 'recorded_at');
        const createdBy = summaryText(item, 'created_by')
          ?? summaryText(item, 'recorded_by');
        const justification = summaryText(item, 'justification');
        return (
          <li
            key={item.result_id}
            className="rounded-lg border border-surface-200 bg-surface-50/70 p-3 dark:border-surface-700 dark:bg-surface-800/40"
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="flex flex-wrap items-center gap-2">
                <span className="text-xs font-semibold text-surface-800 dark:text-surface-100">
                  {edition == null ? 'Legacy' : `Edition ${edition}`}
                </span>
                <ValidationCycleStatusBadge state={state} />
              </span>
              {createdAt && (
                <time className="text-[11px] text-surface-500 dark:text-surface-400">
                  {formatTimestamp(createdAt)}
                </time>
              )}
            </div>
            {score !== null && (
              <p className="mt-1 text-xs text-surface-600 dark:text-surface-300">
                Score {formatScore(score)} of {formatScore(scaleMaximum)}
                {createdBy ? ` · evaluated by ${createdBy}` : ''}
              </p>
            )}
            {justification && (
              <p className="mt-1 text-xs text-surface-600 dark:text-surface-300">
                {justification}
              </p>
            )}
          </li>
        );
      })}
    </ol>
  );
}

export interface QualityPanelProps {
  subjectType: QualitySubjectType;
  subjectId: string;
  subjectVersion: number;
  /** Human validation edition. Defaults to 1 for legacy hosts. */
  subjectEdition?: number;
  subjectStatus: IdeationStatus | RefinementStatus | SpecStatus;
  subjectArchived: boolean;
  canRead: boolean;
  canAssess: boolean;
  canProposeQuestions: boolean;
  /**
   * Requirement text by stable structured-child id (fr_/ac_/tr_...). When
   * provided, each anchored finding quotes the actual requirement instead
   * of exposing only its opaque id.
   */
  anchorTexts?: Record<string, string>;
  onAssessmentRecorded?: () => void;
  onOpenHelp?: () => void;
  refreshKey?: number;
  /** Edition-first UI; the legacy technical evidence view remains opt-in. */
  presentationMode?: 'legacy' | 'lifecycle-edition';
  /** Suppresses the repeated title when rendered inside a validation row. */
  embedded?: boolean;
}

function RequirementLintAdvisoryNotice({
  onOpenHelp,
}: {
  onOpenHelp?: () => void;
}) {
  return (
    <section
      className="rounded-lg border border-blue-200 bg-blue-50/70 p-3 text-blue-800 dark:border-blue-800/60 dark:bg-blue-950/25 dark:text-blue-200"
      data-testid="quality-advisory-notice"
    >
      <h4 className="flex items-center gap-1.5 text-sm font-semibold">
        <Info size={16} aria-hidden="true" />
        Advisory requirement lint
      </h4>
      <p className="mt-1 text-xs">
        An external agent evaluates the requirements for the current edition
        and submits the result to Pulse. An accepted result for the current
        edition is required to continue. Individual findings remain advisory
        and do not block by count or severity.
      </p>
      {onOpenHelp && (
        <button
          type="button"
          onClick={onOpenHelp}
          className="mt-2 inline-flex text-xs font-semibold text-blue-700 underline decoration-blue-300 underline-offset-2 hover:text-blue-900 dark:text-blue-200 dark:hover:text-white"
        >
          How is requirement lint calculated?
        </button>
      )}
    </section>
  );
}

export function QualityPanel({
  subjectType,
  subjectId,
  subjectVersion,
  subjectEdition = 1,
  subjectStatus,
  subjectArchived,
  canRead,
  canAssess,
  canProposeQuestions,
  anchorTexts,
  onAssessmentRecorded,
  onOpenHelp,
  refreshKey = 0,
  presentationMode = 'legacy',
  embedded = false,
}: QualityPanelProps) {
  const kinds: VisibleQualityAssessmentKind[] = subjectType === 'spec'
    ? ['requirement_lint']
    : ['ambiguity'];
  const api = useDashboardApi();
  const apiRef = useRef(api);
  apiRef.current = api;
  const [kind, setKind] = useState<VisibleQualityAssessmentKind>(kinds[0]);
  const [current, setCurrent] = useState<CurrentQualityAssessment | null>(null);
  const [cycleSummary, setCycleSummary] =
    useState<QualityValidationCycleSummary | null>(null);
  const [history, setHistory] = useState<PageEnvelope<QualityAssessmentListItem>>(
    () => emptyPage(),
  );
  const [lifecycleHistory, setLifecycleHistory] =
    useState<ValidationCycleResultSummary[]>([]);
  const [findings, setFindings] = useState<PageEnvelope<QualityFinding>>(
    () => emptyPage(),
  );
  const [historyPage, setHistoryPage] = useState(1);
  const [historyPageSize, setHistoryPageSize] = useState<PaginationPageSize>(25);
  const [findingPage, setFindingPage] = useState(1);
  const [findingPageSize, setFindingPageSize] = useState<PaginationPageSize>(25);
  const [historyState, setHistoryState] = useState<QualityAssessmentReceiptState | ''>('');
  const [findingSeverity, setFindingSeverity] = useState<QualityFindingSeverity | ''>('');
  const [findingCategory, setFindingCategory] = useState('');
  const [currentReceiptOnly, setCurrentReceiptOnly] = useState(false);
  const [historyExpanded, setHistoryExpanded] = useState(false);
  const [findingsExpanded, setFindingsExpanded] = useState(false);
  const [technicalAuditExpanded, setTechnicalAuditExpanded] = useState(false);
  const [technicalAudit, setTechnicalAudit] =
    useState<ValidationTechnicalAudit | null>(null);
  const [technicalAuditLoading, setTechnicalAuditLoading] = useState(false);
  const [technicalAuditError, setTechnicalAuditError] = useState<string | null>(null);
  const [loading, setLoading] = useState(canRead);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const cycleSummaryCacheRef = useRef<{
    key: string;
    value: QualityValidationCycleSummary;
  } | null>(null);
  const lifecycleHistoryLoadKeyRef = useRef<string | null>(null);
  const lifecycleFindingsLoadKeyRef = useRef<string | null>(null);
  const technicalAuditLoadKeyRef = useRef<string | null>(null);

  useEffect(() => {
    if (!kinds.includes(kind)) setKind(kinds[0]);
  }, [kind, kinds]);

  useEffect(() => {
    if (!canRead) {
      setCurrent(null);
      setCycleSummary(null);
      setHistory(emptyPage());
      setLifecycleHistory([]);
      setFindings(emptyPage());
      setLoading(false);
      setError(null);
      return;
    }

    const controller = new AbortController();
    setLoading(true);
    setError(null);
    const historyOffset = (historyPage - 1) * historyPageSize;
    const findingOffset = (findingPage - 1) * findingPageSize;
    void (async () => {
      if (
        presentationMode === 'lifecycle-edition'
        && subjectType !== 'spec'
      ) {
        const cycleKey = [
          subjectType,
          subjectId,
          subjectEdition,
          refreshKey,
          reloadKey,
        ].join(':');
        let cycle = cycleSummaryCacheRef.current?.key === cycleKey
          ? cycleSummaryCacheRef.current.value
          : null;
        if (!cycle) {
          setCycleSummary(null);
          setHistory(emptyPage());
          setLifecycleHistory([]);
          setFindings(emptyPage());
          lifecycleHistoryLoadKeyRef.current = null;
          lifecycleFindingsLoadKeyRef.current = null;
          const resolved = await apiRef.current.getValidationCycle(
            subjectType,
            subjectId,
            { includePrevious: false, signal: controller.signal },
          );
          if (
            resolved.subject_type !== subjectType
            || resolved.subject_id !== subjectId
            || resolved.edition !== subjectEdition
          ) {
            throw new Error(
              'The validation-cycle summary does not match this subject edition.',
            );
          }
          cycle = resolved;
          cycleSummaryCacheRef.current = { key: cycleKey, value: resolved };
        }
        if (controller.signal.aborted) return;
        setCurrent(null);
        setCycleSummary(cycle);

        const currentResult = cycle.current_result?.subject_edition === subjectEdition
          && cycle.current_result.result_type === 'ambiguity_assessment'
          ? cycle.current_result
          : null;
        const historyLoadKey = [
          cycleKey,
          historyPage,
          historyPageSize,
        ].join(':');
        const findingsLoadKey = [
          cycleKey,
          currentResult?.result_id ?? 'none',
          findingPage,
          findingPageSize,
          findingCategory,
          findingSeverity,
        ].join(':');
        const historyRequest = historyExpanded
          && lifecycleHistoryLoadKeyRef.current !== historyLoadKey
          ? apiRef.current.getValidationCycle(subjectType, subjectId, {
              includePrevious: true,
              offset: historyOffset,
              limit: historyPageSize,
              signal: controller.signal,
            })
          : null;
        const findingsRequest = findingsExpanded
          && currentResult
          && lifecycleFindingsLoadKeyRef.current !== findingsLoadKey
          ? apiRef.current.listQualityFindings(subjectType, subjectId, {
              offset: findingOffset,
              limit: findingPageSize,
              assessmentKind: kind,
              receiptId: currentResult.result_id,
              categoryCode: findingCategory || undefined,
              severity: findingSeverity || undefined,
              subjectEdition,
              signal: controller.signal,
            })
          : null;
        const [historyResult, findingResult] = await Promise.all([
          historyRequest,
          findingsRequest,
        ]);
        if (controller.signal.aborted) return;
        if (historyResult) {
          if (
            historyResult.subject_type !== subjectType
            || historyResult.subject_id !== subjectId
            || historyResult.edition !== subjectEdition
          ) {
            throw new Error(
              'The validation-cycle history does not match this subject edition.',
            );
          }
          lifecycleHistoryLoadKeyRef.current = historyLoadKey;
          setLifecycleHistory(historyResult.previous_results.filter(
            (result) => result.result_type === 'ambiguity_assessment',
          ));
          setCycleSummary(historyResult);
        }
        if (findingResult) {
          lifecycleFindingsLoadKeyRef.current = findingsLoadKey;
          setFindings(findingResult);
        }
        return;
      }

      setCycleSummary(null);
      const currentResult = await apiRef.current.getCurrentQualityAssessment(
        subjectType,
        subjectId,
        kind,
        controller.signal,
        presentationMode === 'lifecycle-edition' ? subjectEdition : undefined,
      );
      if (controller.signal.aborted) return;
      setCurrent(currentResult);

      const currentForRequestedEdition = presentationMode === 'lifecycle-edition'
        && currentResult?.lifecycle_state === 'current'
        && currentResult.currentness === 'current'
        && currentResult.edition === subjectEdition
        ? currentResult
        : null;

      const findingsRequest = !findingsExpanded
        ? Promise.resolve(emptyPage<QualityFinding>())
        : presentationMode === 'lifecycle-edition' && !currentForRequestedEdition
          ? Promise.resolve(emptyPage<QualityFinding>())
        : presentationMode === 'legacy' && currentReceiptOnly && !currentResult
        ? Promise.resolve(emptyPage<QualityFinding>())
        : apiRef.current.listQualityFindings(subjectType, subjectId, {
            offset: findingOffset,
            limit: findingPageSize,
            assessmentKind: kind,
            receiptId: presentationMode === 'lifecycle-edition'
              ? currentForRequestedEdition?.receipt.id
              : currentReceiptOnly
                ? currentResult?.receipt.id
                : undefined,
            categoryCode: findingCategory || undefined,
            severity: findingSeverity || undefined,
            subjectEdition: presentationMode === 'lifecycle-edition'
              ? subjectEdition
              : undefined,
            signal: controller.signal,
          });
      const [historyResult, findingResult] = await Promise.all([
        historyExpanded
          ? apiRef.current.listQualityAssessments(subjectType, subjectId, {
              offset: historyOffset,
              limit: historyPageSize,
              assessmentKind: kind,
              state: presentationMode === 'legacy'
                ? historyState || undefined
                : undefined,
              signal: controller.signal,
            })
          : Promise.resolve(emptyPage<QualityAssessmentListItem>()),
        findingsRequest,
      ]);
      if (controller.signal.aborted) return;
      setHistory(historyResult);
      setFindings(findingResult);
    })().catch((reason: unknown) => {
      if (!controller.signal.aborted) setError(getErrorMessage(reason));
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => controller.abort();
  }, [
    canRead,
    currentReceiptOnly,
    findingCategory,
    findingsExpanded,
    findingPage,
    findingPageSize,
    findingSeverity,
    historyPage,
    historyPageSize,
    historyState,
    historyExpanded,
    kind,
    reloadKey,
    refreshKey,
    presentationMode,
    subjectEdition,
    subjectId,
    subjectType,
  ]);

  useEffect(() => {
    if (presentationMode !== 'lifecycle-edition' || !technicalAuditExpanded) {
      return undefined;
    }
    const summarizedResult = cycleSummary?.current_result?.subject_edition === subjectEdition
      ? cycleSummary.current_result
      : null;
    const legacyResult = current?.lifecycle_state === 'current'
      && current.currentness === 'current'
      && current.edition === subjectEdition
      ? current
      : null;
    const resultId = summarizedResult?.result_id ?? legacyResult?.receipt.id;
    const resultType = kind === 'requirement_lint'
      ? 'requirement_lint'
      : 'ambiguity_assessment';
    if (!resultId) {
      setTechnicalAudit(null);
      setTechnicalAuditError(null);
      setTechnicalAuditLoading(false);
      return undefined;
    }
    const loadKey = [
      subjectType,
      subjectId,
      subjectEdition,
      resultType,
      resultId,
    ].join(':');
    if (technicalAuditLoadKeyRef.current === loadKey) return undefined;

    const controller = new AbortController();
    setTechnicalAuditLoading(true);
    setTechnicalAuditError(null);
    apiRef.current.getValidationTechnicalAudit(
      subjectType,
      subjectId,
      resultId,
      resultType,
      controller.signal,
    ).then((audit) => {
      if (controller.signal.aborted) return;
      if (
        audit.result_id !== resultId
        || audit.subject_type !== subjectType
        || audit.subject_id !== subjectId
        || audit.result_type !== resultType
        || audit.subject_edition !== subjectEdition
      ) {
        throw new Error(
          'The technical audit does not match the current validation result.',
        );
      }
      technicalAuditLoadKeyRef.current = loadKey;
      setTechnicalAudit(audit);
    }).catch((reason: unknown) => {
      if (!controller.signal.aborted) {
        setTechnicalAudit(null);
        setTechnicalAuditError(getErrorMessage(reason));
      }
    }).finally(() => {
      if (!controller.signal.aborted) setTechnicalAuditLoading(false);
    });
    return () => controller.abort();
  }, [
    current,
    cycleSummary,
    presentationMode,
    subjectEdition,
    subjectId,
    subjectType,
    technicalAuditExpanded,
  ]);

  if (!canRead) return null;

  const resetForKind = (next: VisibleQualityAssessmentKind) => {
    setCurrent(null);
    setHistory(emptyPage());
    setFindings(emptyPage());
    setKind(next);
    setHistoryPage(1);
    setFindingPage(1);
    setCurrentReceiptOnly(false);
  };

  const reload = () => {
    setReloadKey((value) => value + 1);
    onAssessmentRecorded?.();
  };

  const acceptedAssessmentState = (
    !subjectArchived
    && (
      (subjectType === 'ideation' && subjectStatus === 'evaluating')
      || (subjectType === 'refinement' && subjectStatus === 'approved')
    )
  );
  const canWriteAssessment = canAssess && acceptedAssessmentState;
  const writeUnavailableReason = subjectType === 'spec'
    ? 'Read-only: an external agent records requirement lint after the Spec enters its validation stage.'
    : subjectArchived
      ? 'Read-only: archived subjects cannot receive manual quality assessments.'
      : !canAssess
        ? 'Read-only: your effective board permissions do not allow recording assessments.'
        : subjectType === 'ideation'
          ? 'Read-only: manual ambiguity assessment is available only while the Ideation is Evaluating.'
          : 'Read-only: manual ambiguity assessment is available only while the Refinement is Approved.';

  if (presentationMode === 'lifecycle-edition') {
    const currentForEdition = current?.lifecycle_state === 'current'
      && current.currentness === 'current'
      && current.edition === subjectEdition
      ? current
      : null;
    const summarizedCurrent = cycleSummary?.current_result?.subject_edition === subjectEdition
      && cycleSummary.current_result.result_type === 'ambiguity_assessment'
      ? cycleSummary.current_result
      : null;
    const hasCurrent = Boolean(currentForEdition || summarizedCurrent);
    const lifecycleState = summarizedCurrent
      ? lifecycleSummaryState(summarizedCurrent)
      : lifecycleQualityState(currentForEdition, kind);
    const currentResultId = summarizedCurrent?.result_id
      ?? currentForEdition?.receipt.id;
    const summaryScore = summaryNumber(summarizedCurrent, 'score');
    const summaryThreshold = summaryNumber(summarizedCurrent, 'threshold');
    const summaryCreatedAt = summaryText(summarizedCurrent, 'created_at')
      ?? summaryText(summarizedCurrent, 'recorded_at');
    const summaryCreatedBy = summaryText(summarizedCurrent, 'created_by')
      ?? summaryText(summarizedCurrent, 'recorded_by');
    const summaryJustification = summaryText(summarizedCurrent, 'justification');
    const summaryHeadline = summaryText(summarizedCurrent, 'headline')
      ?? (lifecycleState === 'passed'
        ? 'Ambiguity within the allowed limit'
        : lifecycleState === 'failed'
          ? 'Ambiguity exceeds the allowed limit'
          : lifecycleState === 'needs_attention'
            ? 'Ambiguity needs attention'
            : 'Ambiguity assessment complete');
    const summaryCardTone = lifecycleState === 'passed'
      ? 'border-emerald-200 bg-emerald-50/60 dark:border-emerald-800 dark:bg-emerald-950/20'
      : lifecycleState === 'failed'
        ? 'border-red-200 bg-red-50/60 dark:border-red-800 dark:bg-red-950/20'
        : lifecycleState === 'needs_attention'
          ? 'border-amber-200 bg-amber-50/60 dark:border-amber-800 dark:bg-amber-950/20'
          : 'border-surface-200 bg-white dark:border-surface-700 dark:bg-surface-900/30';
    const previousCount = cycleSummary?.previous_result_count
      ?? (historyExpanded
        ? history.items.filter(
            (item) => item.receipt.id !== currentResultId,
          ).length
        : undefined);
    const title = kind === 'requirement_lint'
      ? 'Requirement lint'
      : 'Ambiguity assessment';

    return (
      <div className="space-y-4" data-testid="quality-panel" data-presentation="lifecycle-edition">
        {!embedded && <ValidationCycleHeader
          title={title}
          edition={subjectEdition}
          description={kind === 'requirement_lint'
            ? 'One current lint result is kept for each validation edition.'
            : 'One current ambiguity result is kept for each lifecycle edition.'}
          icon={(
            <ClipboardCheck
              size={18}
              className={kind === 'requirement_lint'
                ? 'text-blue-600 dark:text-blue-400'
                : 'text-violet-600 dark:text-violet-300'}
              aria-hidden="true"
            />
          )}
          actions={(
            <button
              type="button"
              onClick={() => setReloadKey((value) => value + 1)}
              disabled={loading}
              className="inline-flex min-h-8 items-center gap-1 rounded-lg border border-surface-300 bg-white px-2.5 py-1 text-xs text-surface-700 hover:bg-surface-100 disabled:opacity-50 dark:border-surface-600 dark:bg-surface-800 dark:text-surface-200"
            >
              <RefreshCw size={13} className={loading ? 'animate-spin' : ''} aria-hidden="true" />
              Refresh
            </button>
          )}
        />}

        {error && (
          <div
            role="alert"
            className="rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-700 dark:border-red-800 dark:bg-red-950/30 dark:text-red-300"
          >
            Could not load the current result. {error}
          </div>
        )}

        <section
          className={`rounded-xl border p-4 ${
            currentForEdition
              ? currentReceiptTone(currentForEdition, kind).card
              : summarizedCurrent
                ? summaryCardTone
                : 'border-surface-200 bg-white dark:border-surface-700 dark:bg-surface-900/30'
          }`}
          data-testid="quality-current-result"
          aria-busy={loading && !hasCurrent}
        >
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <p className="text-[11px] font-semibold uppercase tracking-wide text-surface-500 dark:text-surface-400">
                Current assessment
              </p>
              <h4 className="mt-1 text-sm font-semibold text-surface-900 dark:text-white">
                {currentForEdition
                  ? kind === 'requirement_lint'
                    ? currentForEdition.receipt.score === 0
                      ? 'No lint findings'
                      : `${formatScore(currentForEdition.receipt.score)} lint finding${currentForEdition.receipt.score === 1 ? '' : 's'}`
                    : currentReceiptHeadline(currentForEdition, kind)
                  : summarizedCurrent
                    ? summaryHeadline
                  : loading
                    ? 'Loading current assessment…'
                    : `No result for Edition ${subjectEdition}`}
              </h4>
            </div>
            <ValidationCycleStatusBadge
              state={loading && !hasCurrent ? 'in_progress' : lifecycleState}
              testId="quality-current-status"
            />
          </div>

          {currentForEdition ? (
            <div className="mt-4 flex flex-wrap items-center gap-4">
              <QualityScoreRing assessment={currentForEdition} kind={kind} />
              <div className="min-w-0 flex-1">
                <p className="text-xs text-surface-700 dark:text-surface-200">
                  {kind === 'requirement_lint'
                    ? `${formatScore(currentForEdition.receipt.scale.maximum)} rules evaluated · lower is better`
                    : currentForEdition.gate_preview.threshold == null
                      ? `Scale ${currentForEdition.receipt.scale.minimum}–${currentForEdition.receipt.scale.maximum}`
                      : `Maximum accepted score ${currentForEdition.gate_preview.threshold}`}
                </p>
                <p className="mt-1 text-[11px] text-surface-500 dark:text-surface-400">
                  Evaluated {formatTimestamp(currentForEdition.receipt.created_at)} by{' '}
                  {currentForEdition.receipt.created_by}
                </p>
                {currentForEdition.receipt.justification && (
                  <p className="mt-2 text-xs text-surface-600 dark:text-surface-300">
                    {currentForEdition.receipt.justification}
                  </p>
                )}
              </div>
            </div>
          ) : summarizedCurrent ? (
            <div className="mt-4 flex flex-wrap items-center gap-4">
              {summaryScore !== null && (
                <div
                  role="img"
                  aria-label={`Ambiguity score ${formatScore(summaryScore)} out of 5`}
                  className={`flex h-20 w-20 shrink-0 items-center justify-center rounded-full border-4 ${
                    lifecycleState === 'passed'
                      ? 'border-emerald-400 text-emerald-700 dark:text-emerald-300'
                      : lifecycleState === 'failed'
                        ? 'border-red-400 text-red-700 dark:text-red-300'
                        : 'border-amber-400 text-amber-700 dark:text-amber-200'
                  }`}
                >
                  <span aria-hidden="true" className="text-2xl font-bold">
                    {formatScore(summaryScore)}
                    <span className="text-sm text-surface-400">/5</span>
                  </span>
                </div>
              )}
              <div className="min-w-0 flex-1">
                {summaryThreshold !== null && (
                  <p className="text-xs text-surface-700 dark:text-surface-200">
                    Maximum accepted score {formatScore(summaryThreshold)}
                  </p>
                )}
                {(summaryCreatedAt || summaryCreatedBy) && (
                  <p className="mt-1 text-[11px] text-surface-500 dark:text-surface-400">
                    {summaryCreatedAt
                      ? `Evaluated ${formatTimestamp(summaryCreatedAt)}`
                      : 'Evaluated'}
                    {summaryCreatedBy ? ` by ${summaryCreatedBy}` : ''}
                  </p>
                )}
                {summaryJustification && (
                  <p className="mt-2 text-xs text-surface-600 dark:text-surface-300">
                    {summaryJustification}
                  </p>
                )}
              </div>
            </div>
          ) : !loading && (
            <p className="mt-3 max-w-2xl text-xs text-surface-500 dark:text-surface-400">
              This edition has not been assessed yet. A new result is recorded
              when the entity enters its validation stage.
            </p>
          )}
        </section>

        {kind === 'requirement_lint' && (
          <RequirementLintAdvisoryNotice onOpenHelp={onOpenHelp} />
        )}

        {subjectType !== 'spec' && (
          canWriteAssessment ? (
            <ManualAssessmentForm
              subjectType={subjectType}
              subjectId={subjectId}
              subjectVersion={
                cycleSummary?.submission_fence.expected_subject_version
                ?? subjectVersion
              }
              subjectEdition={
                cycleSummary?.submission_fence.expected_validation_edition
                ?? subjectEdition
              }
              expectedHeadRevision={
                cycleSummary?.submission_fence.expected_head_revision
                ?? currentForEdition?.head_revision
                ?? 0
              }
              canProposeQuestions={canProposeQuestions}
              disabled={loading || Boolean(error)}
              onRecorded={reload}
            />
          ) : (
            <p className="rounded-lg border border-surface-200 bg-surface-50 p-3 text-xs text-surface-600 dark:border-surface-700 dark:bg-surface-900/40 dark:text-surface-300">
              {writeUnavailableReason}
            </p>
          )
        )}

        <CollapsibleEvidenceSection
          title="Findings"
          description="Open the findings only when you need the detailed observations."
          expanded={findingsExpanded}
          onToggle={() => setFindingsExpanded((value) => !value)}
          testId="quality-findings"
        >
          {loading && findings.items.length === 0 ? (
            <p className="text-xs text-surface-500 dark:text-surface-400">
              Loading findings…
            </p>
          ) : findings.items.length === 0 ? (
            <p className="text-xs text-surface-500 dark:text-surface-400">
              No findings were recorded for this edition.
            </p>
          ) : (
            <FindingItems
              page={findings}
              anchorTexts={anchorTexts}
              showTechnicalMetadata={false}
            />
          )}
        </CollapsibleEvidenceSection>

        <PreviousResultsSection
          expanded={historyExpanded}
          onToggle={() => setHistoryExpanded((value) => !value)}
          count={previousCount}
          testId="quality-previous-results"
        >
          {loading && history.items.length === 0 ? (
            <p className="text-xs text-surface-500 dark:text-surface-400">
              Loading previous results…
            </p>
          ) : (
            <LifecyclePreviousQualityResults
              results={lifecycleHistory}
              currentReceiptId={currentResultId}
            />
          )}
          {previousCount !== undefined && previousCount > historyPageSize && (
            <AccessiblePaginator
              page={historyPage}
              pageSize={historyPageSize}
              totalFiltered={previousCount}
              totalOverall={previousCount}
              itemCount={lifecycleHistory.length}
              loading={loading}
              error={error}
              onRetry={() => setReloadKey((value) => value + 1)}
              onPaginationChange={(intent) => {
                setHistoryPage(intent.page);
                setHistoryPageSize(intent.pageSize);
              }}
              ariaLabel="Previous validation results pagination"
              emptyMessage="No previous results are available."
              testId="quality-previous-results-paginator"
              compact
            />
          )}
        </PreviousResultsSection>

        <TechnicalAuditSection
          expanded={technicalAuditExpanded}
          onToggle={() => setTechnicalAuditExpanded((value) => !value)}
        >
          {technicalAuditLoading ? (
            <p role="status" className="text-xs text-surface-500 dark:text-surface-400">
              Loading technical audit…
            </p>
          ) : technicalAuditError ? (
            <p role="alert" className="text-xs text-red-700 dark:text-red-300">
              Technical audit could not be loaded. {technicalAuditError}
            </p>
          ) : technicalAudit && technicalAudit.result_id === currentResultId ? (
            <dl className="grid gap-2 text-xs sm:grid-cols-2">
              <div>
                <dt className="text-surface-500 dark:text-surface-400">Result identifier</dt>
                <dd className="mt-0.5 break-all font-mono text-surface-800 dark:text-surface-100">
                  {technicalAudit.result_id}
                </dd>
              </div>
              <div>
                <dt className="text-surface-500 dark:text-surface-400">Processing fence</dt>
                <dd className="mt-0.5 font-mono text-surface-800 dark:text-surface-100">
                  subject r{technicalAudit.technical_audit.subject_version} · head r{technicalAudit.technical_audit.head_revision}
                </dd>
              </div>
              <div className="sm:col-span-2">
                <dt className="text-surface-500 dark:text-surface-400">Immutable record</dt>
                <dd className="mt-0.5 break-all font-mono text-surface-800 dark:text-surface-100">
                  {technicalAudit.technical_audit.receipt_id}
                </dd>
              </div>
            </dl>
          ) : (
            <p className="text-xs text-surface-500 dark:text-surface-400">
              No technical record exists for the current edition.
            </p>
          )}
        </TechnicalAuditSection>
      </div>
    );
  }

  return (
    <div className="space-y-5" data-testid="quality-panel">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="flex items-center gap-2 text-base font-semibold text-surface-900 dark:text-white">
            <ClipboardCheck size={18} className="text-blue-600 dark:text-blue-400" />
            {subjectType === 'spec' ? 'Requirement lint' : 'Quality assessments'}
          </h3>
          <p className="mt-1 text-xs text-surface-500 dark:text-surface-400">
            {subjectType === 'spec'
              ? 'Deterministic advisory findings with immutable receipts and pinpoint evidence.'
              : 'Immutable receipts, currentness, server gate decisions and pinpoint findings.'}
          </p>
        </div>
        <button
          type="button"
          onClick={() => setReloadKey((value) => value + 1)}
          disabled={loading}
          className="inline-flex items-center gap-1 rounded-lg border border-surface-300 bg-white px-2.5 py-1.5 text-xs text-surface-700 hover:bg-surface-100 disabled:opacity-50 dark:border-surface-600 dark:bg-surface-800 dark:text-surface-200"
        >
          <RefreshCw size={13} className={loading ? 'animate-spin' : ''} /> Refresh
        </button>
      </header>

      {kinds.length > 1 && (
        <div
          className="inline-flex rounded-lg border border-surface-200 bg-surface-50 p-1 dark:border-surface-700 dark:bg-surface-900"
          role="tablist"
          aria-label="Quality assessment kind"
        >
          {kinds.map((availableKind) => (
            <button
              key={availableKind}
              type="button"
              role="tab"
              aria-selected={kind === availableKind}
              onClick={() => resetForKind(availableKind)}
              className={`rounded-md px-3 py-1.5 text-xs font-medium ${
                kind === availableKind
                  ? 'bg-white text-blue-700 shadow-sm dark:bg-surface-700 dark:text-blue-200'
                  : 'text-surface-500 hover:text-surface-800 dark:text-surface-400 dark:hover:text-surface-100'
              }`}
            >
              {KIND_LABELS[availableKind]}
            </button>
          ))}
        </div>
      )}

      {error && (
        <div
          role="alert"
          className="rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-700 dark:border-red-800 dark:bg-red-950/30 dark:text-red-300"
        >
          Could not load quality data. {error}
        </div>
      )}

      <section className="space-y-2" aria-busy={loading}>
        <h3 className="text-sm font-semibold text-surface-800 dark:text-surface-100">
          Current receipt
        </h3>
        {loading && !current ? (
          <p className="rounded-lg border border-surface-200 p-3 text-xs text-surface-500 dark:border-surface-700 dark:text-surface-400">
            Loading current assessment…
          </p>
        ) : current ? (
          <>
            <div className={`rounded-xl border p-4 ${currentReceiptTone(current, kind).card}`}>
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="flex items-center gap-4">
                  <QualityScoreRing assessment={current} kind={kind} />
                  <div>
                    <div className="flex flex-wrap items-center gap-2">
                      <CurrentReceiptStatusIcon assessment={current} kind={kind} />
                      <strong className="text-sm text-surface-800 dark:text-surface-100">
                        {currentReceiptHeadline(current, kind)}
                      </strong>
                      <span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${currentReceiptTone(current, kind).badge}`}>
                        {current.currentness === 'current' ? 'Current receipt' : 'Stale receipt'}
                      </span>
                    </div>
                    <p
                      className="mt-1 text-xs text-surface-500 dark:text-surface-400"
                      data-testid={kind === 'requirement_lint'
                        ? 'requirement-lint-summary'
                        : undefined}
                    >
                      {kind === 'requirement_lint'
                        ? (
                          <>
                            <strong className="text-surface-700 dark:text-surface-200">
                              {formatScore(current.receipt.score)}
                            </strong>
                            {' '}finding{current.receipt.score === 1 ? '' : 's'} across{' '}
                            <strong className="text-surface-700 dark:text-surface-200">
                              {formatScore(current.receipt.scale.maximum)}
                            </strong>
                            {' '}evaluated rule{current.receipt.scale.maximum === 1 ? '' : 's'} — lower is better
                          </>
                        )
                        : current.gate_preview.threshold === null
                        ? `Scale: ${current.receipt.scale.minimum}–${current.receipt.scale.maximum}`
                        : (
                          <>
                            Maximum tolerated on this board:{' '}
                            <strong className="text-surface-700 dark:text-surface-200">
                              {current.gate_preview.threshold}
                            </strong>
                          </>
                        )}
                    </p>
                  </div>
                </div>
                <span className="rounded-full bg-surface-100 px-2 py-0.5 text-[10px] font-semibold text-surface-600 dark:bg-surface-800 dark:text-surface-300">
                  {current.currentness} · head r{current.head_revision}
                </span>
              </div>
              <p className="mt-3 break-all border-t border-current/10 pt-3 text-[11px] text-surface-500 dark:text-surface-400">
                Receipt {current.receipt.id} · subject v{current.receipt.subject_version}
              </p>
            </div>
          </>
        ) : (
          <p
            className="rounded-lg border border-dashed border-surface-300 p-3 text-xs text-surface-500 dark:border-surface-700 dark:text-surface-400"
            data-testid="quality-current-empty"
          >
            No current {KIND_LABELS[kind].toLowerCase()} assessment.
          </p>
        )}
      </section>

      {kind === 'requirement_lint' ? (
        <RequirementLintAdvisoryNotice onOpenHelp={onOpenHelp} />
      ) : current?.gate_preview.applicable ? (
        <QualityGatePreviewCard assessment={current} />
      ) : null}

      {subjectType !== 'spec' && (
        canWriteAssessment ? (
          <ManualAssessmentForm
            subjectType={subjectType}
            subjectId={subjectId}
            subjectVersion={subjectVersion}
            subjectEdition={subjectEdition}
            expectedHeadRevision={current?.head_revision ?? 0}
            canProposeQuestions={canProposeQuestions}
            disabled={loading || Boolean(error)}
            onRecorded={reload}
          />
        ) : (
          <p
            className="rounded-lg border border-surface-200 bg-surface-50 p-3 text-xs text-surface-600 dark:border-surface-700 dark:bg-surface-900/40 dark:text-surface-300"
            data-testid="quality-read-only"
          >
            {writeUnavailableReason}
          </p>
        )
      )}

      <CollapsibleEvidenceSection
        title="Receipt history"
        description="History is append-only; superseded and stale receipts remain traceable."
        expanded={historyExpanded}
        onToggle={() => setHistoryExpanded((value) => !value)}
        testId="quality-history"
      >
        <div className="flex flex-wrap items-end justify-end gap-2">
          <label className="text-xs text-surface-600 dark:text-surface-300">
            State
            <select
              value={historyState}
              onChange={(event) => {
                setHistoryState(event.target.value as QualityAssessmentReceiptState | '');
                setHistoryPage(1);
              }}
              className="ml-2 min-h-8 rounded-lg border border-surface-300 bg-white px-2 py-1 text-xs dark:border-surface-600 dark:bg-surface-800"
            >
              <option value="">All</option>
              <option value="current">Current</option>
              <option value="stale">Stale</option>
              <option value="superseded">Superseded</option>
            </select>
          </label>
        </div>
        <HistoryItems page={history} />
        <AccessiblePaginator
          page={historyPage}
          pageSize={historyPageSize}
          totalFiltered={history.total_filtered}
          totalOverall={history.total_overall}
          itemCount={history.items.length}
          loading={loading}
          error={error}
          onRetry={() => setReloadKey((value) => value + 1)}
          onPaginationChange={(intent) => {
            setHistoryPage(intent.page);
            setHistoryPageSize(intent.pageSize);
          }}
          ariaLabel="Quality receipt history pagination"
          emptyMessage="No matching assessment receipts."
          testId="quality-history-paginator"
          compact
        />
      </CollapsibleEvidenceSection>

      <CollapsibleEvidenceSection
        title="Pinpoint findings"
        description="Filtered server-side without loading receipts one by one."
        expanded={findingsExpanded}
        onToggle={() => setFindingsExpanded((value) => !value)}
        testId="quality-findings"
      >
        <div className="grid gap-2 sm:grid-cols-3">
          <label className="text-xs text-surface-600 dark:text-surface-300">
            Severity
            <select
              value={findingSeverity}
              onChange={(event) => {
                setFindingSeverity(event.target.value as QualityFindingSeverity | '');
                setFindingPage(1);
              }}
              className="mt-1 block min-h-9 w-full rounded-lg border border-surface-300 bg-white px-2 py-1 text-xs dark:border-surface-600 dark:bg-surface-800"
            >
              <option value="">All</option>
              {SEVERITIES.map((severity) => (
                <option key={severity} value={severity}>{severity}</option>
              ))}
            </select>
          </label>
          <label className="text-xs text-surface-600 dark:text-surface-300">
            Category
            <select
              value={findingCategory}
              onChange={(event) => {
                setFindingCategory(event.target.value);
                setFindingPage(1);
              }}
              className="mt-1 block min-h-9 w-full rounded-lg border border-surface-300 bg-white px-2 py-1 text-xs dark:border-surface-600 dark:bg-surface-800"
            >
              <option value="">All</option>
              {CATEGORY_OPTIONS.map(([value, label]) => (
                <option key={value} value={value}>{label}</option>
              ))}
            </select>
          </label>
          <label className="flex min-h-9 items-center gap-2 self-end rounded-lg border border-surface-300 bg-white px-2.5 py-1.5 text-xs text-surface-600 dark:border-surface-600 dark:bg-surface-800 dark:text-surface-300">
            <input
              type="checkbox"
              checked={currentReceiptOnly}
              onChange={(event) => {
                setCurrentReceiptOnly(event.target.checked);
                setFindingPage(1);
              }}
              disabled={!current}
            />
            Current receipt only
          </label>
        </div>
        <FindingItems page={findings} anchorTexts={anchorTexts} />
        <AccessiblePaginator
          page={findingPage}
          pageSize={findingPageSize}
          totalFiltered={findings.total_filtered}
          totalOverall={findings.total_overall}
          itemCount={findings.items.length}
          loading={loading}
          error={error}
          onRetry={() => setReloadKey((value) => value + 1)}
          onPaginationChange={(intent) => {
            setFindingPage(intent.page);
            setFindingPageSize(intent.pageSize);
          }}
          ariaLabel="Quality findings pagination"
          emptyMessage="No matching pinpoint findings."
          testId="quality-findings-paginator"
          compact
        />
      </CollapsibleEvidenceSection>
    </div>
  );
}

export default QualityPanel;
