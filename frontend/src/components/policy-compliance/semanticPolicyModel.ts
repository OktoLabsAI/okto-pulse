import type {
  CreatedSemanticSkipResponse,
  PolicyCurrentness,
  PolicyEntityType,
  PolicyProjection,
  SemanticAssessmentCurrentnessReason,
  SemanticAssessmentCurrentV2,
  SemanticAssessmentDetail,
  SemanticAssessmentListItem,
  SemanticCursorPage,
  SemanticEvidenceRef,
  SemanticFindingDetail,
  SemanticFindingListItem,
  SemanticMetricOutcome,
  SemanticMetricResultV2,
  SemanticMetricResultDetail,
  SemanticCurrentAssessmentResponse,
  SemanticAnchorV2,
  SemanticPinpointV2,
  SemanticPinpoint,
  SemanticSkipDetail,
  SemanticSkipListItem,
  SemanticWaiverDetail,
  SemanticWaiverListItem,
  RequestedSemanticWaiverResponse,
  RevokedSemanticSkipResponse,
} from '@/types/policy-governance';

const CURRENTNESS_REASONS =
  new Set<SemanticAssessmentCurrentnessReason>([
    'current_snapshot_missing',
    'subject_version_changed',
    'subject_content_changed',
    'guideline_revision_changed',
    'guideline_revision_digest_changed',
    'binding_revision_changed',
    'binding_configuration_changed',
    'policy_set_changed',
    'binding_head_changed',
    'input_digest_changed',
  ]);

const ASSESSMENT_DETAIL_FIELDS = [
  'projection',
  'receipt_id',
  'board_id',
  'entity_type',
  'subject_id',
  'subject_version',
  'validation_edition',
  'binding_id',
  'guideline_id',
  'guideline_revision_id',
  'enforcement',
  'state',
  'currentness',
  'currentness_reasons',
  'confidence',
  'minimum_confidence',
  'metric_count',
  'failed_metric_count',
  'recorded_at',
  'binding_revision',
  'assessor_agent_id',
  'assessor_model_id',
  'assessor_independent',
  'confidence_admissible',
  'metric_results',
] as const;

const METRIC_RESULT_DETAIL_FIELDS = [
  'metric_result_id',
  'metric_id',
  'metric_code',
  'score',
  'direction',
  'default_threshold',
  'effective_threshold',
  'threshold_source',
  'outcome',
  'rationale',
  'evidence_refs',
  'pinpoints',
] as const;

const EVIDENCE_FIELDS = [
  'source_type',
  'source_id',
  'source_version',
  'content_hash',
] as const;

const PINPOINT_FIELDS = [
  'anchor_type',
  'anchor_ref',
  'excerpt_hash',
  'input_digest',
] as const;

const FINDING_DETAIL_FIELDS = [
  'projection',
  'finding_id',
  'receipt_id',
  'board_id',
  'entity_type',
  'subject_id',
  'subject_version',
  'guideline_id',
  'guideline_revision_id',
  'binding_id',
  'metric_id',
  'metric_code',
  'currentness',
  'currentness_reasons',
  'created_at',
  'metric_result_id',
  'binding_revision',
  'rationale',
  'evidence_refs',
  'pinpoints',
] as const;

const WAIVER_DETAIL_FIELDS = [
  'projection',
  'waiver_id',
  'board_id',
  'entity_type',
  'subject_id',
  'subject_version',
  'finding_id',
  'receipt_id',
  'guideline_id',
  'guideline_revision_id',
  'binding_id',
  'metric_id',
  'metric_code',
  'status',
  'waiver_revision',
  'currentness',
  'currentness_reasons',
  'requested_at',
  'expires_at',
  'last_event_type',
  'last_event_at',
  'justification',
  'requested_by',
  'original_expires_at',
  'reviewed_by',
  'reviewed_at',
  'review_reason',
  'revoked_by',
  'revoked_at',
  'expire_reason',
  'evidence_refs',
] as const;

const SKIP_DETAIL_FIELDS = [
  'projection',
  'skip_id',
  'board_id',
  'entity_type',
  'subject_id',
  'subject_version',
  'guideline_id',
  'guideline_revision_id',
  'binding_id',
  'status',
  'skip_revision',
  'currentness',
  'currentness_reasons',
  'created_at',
  'last_event_type',
  'last_event_at',
  'binding_revision',
  'reason',
  'created_by',
  'revoked_by',
  'revoked_at',
  'revocation_reason',
] as const;

const PAGE_FIELDS = [
  'items',
  'projection',
  'next_cursor',
  'has_more',
] as const;

const REQUESTED_WAIVER_FIELDS = [
  'waiver_id',
  'status',
  'scope_digest',
] as const;

const CREATED_SKIP_FIELDS = [
  'skip_id',
  'scope_digest',
  'created_by',
] as const;

const REVOKED_SKIP_FIELDS = [
  'skip_id',
  'skip_revision',
  'status',
  'revoked_by',
  'replayed',
] as const;

export interface SemanticSubjectExpectation {
  boardId: string;
  entityType: PolicyEntityType;
  subjectId: string;
  /** Required only for the human lifecycle projection of current evidence. */
  validationEdition?: number;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function exactFields(
  value: Record<string, unknown>,
  fields: readonly string[],
  label: string,
): void {
  const expected = new Set(fields);
  const keys = Object.keys(value);
  if (
    keys.length !== fields.length
    || fields.some((field) => !Object.prototype.hasOwnProperty.call(value, field))
    || keys.some((field) => !expected.has(field))
  ) {
    throw new Error(
      `Semantic guideline ${label} has an unknown or missing field.`,
    );
  }
}

function requiredText(value: unknown, label: string): string {
  if (typeof value !== 'string' || value.trim() === '') {
    throw new Error(`Semantic guideline ${label} is invalid.`);
  }
  return value;
}

function nullableText(value: unknown, label: string): string | null {
  return value === null ? null : requiredText(value, label);
}

function timestamp(value: unknown, label: string): string {
  const text = requiredText(value, label);
  if (Number.isNaN(Date.parse(text))) {
    throw new Error(`Semantic guideline ${label} is invalid.`);
  }
  return text;
}

function nullableTimestamp(value: unknown, label: string): string | null {
  return value === null ? null : timestamp(value, label);
}

function metricCode(value: unknown, label: string): string {
  const text = requiredText(value, label);
  if (!/^[A-Za-z][A-Za-z0-9_.:-]*$/u.test(text)) {
    throw new Error(`Semantic guideline ${label} is invalid.`);
  }
  return text;
}

function sha256(value: unknown, label: string): string {
  const text = requiredText(value, label);
  if (!/^[0-9a-f]{64}$/u.test(text)) {
    throw new Error(`Semantic guideline ${label} is invalid.`);
  }
  return text;
}

function nullableSha256(value: unknown, label: string): string | null {
  return value === null ? null : sha256(value, label);
}

function boundedScore(value: unknown, label: string): number {
  if (
    typeof value !== 'number'
    || !Number.isInteger(value)
    || value < 0
    || value > 100
  ) {
    throw new Error(`Semantic guideline ${label} is outside 0–100.`);
  }
  return value;
}

function nonNegativeInteger(value: unknown, label: string): number {
  if (
    typeof value !== 'number'
    || !Number.isInteger(value)
    || value < 0
  ) {
    throw new Error(`Semantic guideline ${label} is invalid.`);
  }
  return value;
}

function positiveInteger(value: unknown, label: string): number {
  const parsed = nonNegativeInteger(value, label);
  if (parsed === 0) {
    throw new Error(`Semantic guideline ${label} is invalid.`);
  }
  return parsed;
}

function closedUniqueValues<T extends string>(
  value: unknown,
  allowed: ReadonlySet<T>,
  label: string,
): T[] {
  if (!Array.isArray(value)) {
    throw new Error(`Semantic guideline ${label} is invalid.`);
  }
  const result: T[] = [];
  for (const item of value) {
    if (typeof item !== 'string' || !allowed.has(item as T)) {
      throw new Error(`Semantic guideline ${label} is invalid.`);
    }
    if (result.includes(item as T)) {
      throw new Error(`Semantic guideline ${label} contains duplicates.`);
    }
    result.push(item as T);
  }
  return result;
}

function currentness(
  value: Record<string, unknown>,
): {
  currentness: PolicyCurrentness;
  reasons: SemanticAssessmentCurrentnessReason[];
} {
  if (value.currentness !== 'current' && value.currentness !== 'stale') {
    throw new Error('Semantic guideline currentness is invalid.');
  }
  const reasons = closedUniqueValues(
    value.currentness_reasons,
    CURRENTNESS_REASONS,
    'currentness reasons',
  );
  if (
    (value.currentness === 'current' && reasons.length > 0)
    || (value.currentness === 'stale' && reasons.length === 0)
  ) {
    throw new Error('Semantic guideline currentness is inconsistent.');
  }
  return { currentness: value.currentness, reasons };
}

function matchesSubject(
  value: Record<string, unknown>,
  expected: SemanticSubjectExpectation,
): void {
  if (
    value.board_id !== expected.boardId
    || value.entity_type !== expected.entityType
    || value.subject_id !== expected.subjectId
  ) {
    throw new Error(
      'Semantic guideline evidence does not match the active subject.',
    );
  }
  positiveInteger(value.subject_version, 'subject version');
}

function parseEvidence(value: unknown): SemanticEvidenceRef {
  if (!isRecord(value)) {
    throw new Error('Semantic guideline evidence reference is invalid.');
  }
  exactFields(value, EVIDENCE_FIELDS, 'evidence reference');
  return {
    source_type: requiredText(value.source_type, 'evidence source type'),
    source_id: requiredText(value.source_id, 'evidence source identity'),
    source_version: positiveInteger(
      value.source_version,
      'evidence source version',
    ),
    content_hash: sha256(value.content_hash, 'evidence content hash'),
  };
}

function uniqueBy<T>(
  values: T[],
  key: (value: T) => string,
  label: string,
): T[] {
  const keys = values.map(key);
  if (new Set(keys).size !== keys.length) {
    throw new Error(`Semantic guideline ${label} contains duplicates.`);
  }
  return values;
}

function parsePinpoint(value: unknown): SemanticPinpoint {
  if (!isRecord(value)) {
    throw new Error('Semantic guideline pinpoint is invalid.');
  }
  exactFields(value, PINPOINT_FIELDS, 'pinpoint');
  if (
    value.anchor_type !== 'whole_artifact'
    && value.anchor_type !== 'field'
    && value.anchor_type !== 'structured_child'
    && value.anchor_type !== 'qa'
  ) {
    throw new Error('Semantic guideline pinpoint anchor type is invalid.');
  }
  return {
    anchor_type: value.anchor_type,
    anchor_ref: nullableText(value.anchor_ref, 'pinpoint anchor reference'),
    excerpt_hash: nullableSha256(
      value.excerpt_hash,
      'pinpoint excerpt hash',
    ),
    input_digest: sha256(value.input_digest, 'pinpoint input digest'),
  };
}

function parseMetricResult(value: unknown): SemanticMetricResultDetail {
  if (!isRecord(value)) {
    throw new Error('Semantic guideline metric result is invalid.');
  }
  exactFields(value, METRIC_RESULT_DETAIL_FIELDS, 'metric result');
  if (value.direction !== 'minimum' && value.direction !== 'maximum') {
    throw new Error('Semantic guideline metric direction is invalid.');
  }
  if (
    value.threshold_source !== 'default'
    && value.threshold_source !== 'override'
  ) {
    throw new Error('Semantic guideline threshold source is invalid.');
  }
  if (value.outcome !== 'pass' && value.outcome !== 'fail') {
    throw new Error('Semantic guideline metric outcome is invalid.');
  }
  const score = boundedScore(value.score, 'metric score');
  const defaultThreshold = boundedScore(
    value.default_threshold,
    'default threshold',
  );
  const effectiveThreshold = boundedScore(
    value.effective_threshold,
    'effective threshold',
  );
  if (
    value.threshold_source === 'default'
    && effectiveThreshold !== defaultThreshold
  ) {
    throw new Error(
      'Semantic guideline default threshold source is inconsistent.',
    );
  }
  const computed: SemanticMetricOutcome =
    value.direction === 'minimum'
      ? score >= effectiveThreshold ? 'pass' : 'fail'
      : score <= effectiveThreshold ? 'pass' : 'fail';
  if (computed !== value.outcome) {
    throw new Error(
      'Semantic guideline metric score contradicts its authoritative outcome.',
    );
  }
  if (!Array.isArray(value.evidence_refs) || value.evidence_refs.length === 0) {
    throw new Error('Semantic guideline metric evidence is missing.');
  }
  if (!Array.isArray(value.pinpoints) || value.pinpoints.length === 0) {
    throw new Error('Semantic guideline metric pinpoints are missing.');
  }
  return {
    metric_result_id: requiredText(
      value.metric_result_id,
      'metric result identity',
    ),
    metric_id: requiredText(value.metric_id, 'metric identity'),
    metric_code: metricCode(value.metric_code, 'metric code'),
    score,
    direction: value.direction,
    default_threshold: defaultThreshold,
    effective_threshold: effectiveThreshold,
    threshold_source: value.threshold_source,
    outcome: value.outcome,
    rationale: requiredText(value.rationale, 'metric rationale'),
    evidence_refs: uniqueBy(
      value.evidence_refs.map(parseEvidence),
      (item) =>
        `${item.source_type}:${item.source_id}:${item.source_version}:${item.content_hash}`,
      'metric evidence references',
    ),
    pinpoints: uniqueBy(
      value.pinpoints.map(parsePinpoint),
      (item) =>
        `${item.anchor_type}:${item.anchor_ref ?? ''}:${item.excerpt_hash ?? ''}:${item.input_digest}`,
      'metric pinpoints',
    ),
  };
}

export function parseSemanticAssessmentDetail(
  value: SemanticAssessmentListItem | unknown,
  expected: SemanticSubjectExpectation,
): SemanticAssessmentDetail {
  if (!isRecord(value)) {
    throw new Error('Semantic guideline assessment is invalid.');
  }
  exactFields(value, ASSESSMENT_DETAIL_FIELDS, 'assessment');
  if (value.projection !== 'detail') {
    throw new Error('Semantic guideline assessment projection is not detail.');
  }
  matchesSubject(value, expected);
  if (value.enforcement !== 'advisory' && value.enforcement !== 'blocking') {
    throw new Error('Semantic guideline enforcement is invalid.');
  }
  if (
    value.state !== 'passed'
    && value.state !== 'metric_threshold_failed'
  ) {
    throw new Error('Semantic guideline assessment state is invalid.');
  }
  if (
    typeof value.assessor_independent !== 'boolean'
    || typeof value.confidence_admissible !== 'boolean'
  ) {
    throw new Error('Semantic guideline assessor admission is invalid.');
  }
  const resolvedCurrentness = currentness(value);
  const validationEdition = value.validation_edition === null
    ? null
    : positiveInteger(value.validation_edition, 'validation edition');
  if (
    expected.validationEdition !== undefined
    && validationEdition !== expected.validationEdition
  ) {
    throw new Error(
      'Semantic guideline assessment does not match the active validation edition.',
    );
  }
  const confidence = boundedScore(value.confidence, 'confidence');
  const minimumConfidence = boundedScore(
    value.minimum_confidence,
    'minimum confidence',
  );
  if (value.confidence_admissible !== (confidence >= minimumConfidence)) {
    throw new Error(
      'Semantic guideline confidence admission contradicts its threshold.',
    );
  }
  if (!Array.isArray(value.metric_results)) {
    throw new Error('Semantic guideline metric results are invalid.');
  }
  const metricResults = value.metric_results.map(parseMetricResult);
  const metricIds = metricResults.map((item) => item.metric_id);
  const resultIds = metricResults.map((item) => item.metric_result_id);
  const metricCodes = metricResults.map((item) => item.metric_code);
  if (
    new Set(metricIds).size !== metricIds.length
    || new Set(resultIds).size !== resultIds.length
    || new Set(metricCodes).size !== metricCodes.length
  ) {
    throw new Error('Semantic guideline assessment repeats a metric result.');
  }
  const metricCount = nonNegativeInteger(value.metric_count, 'metric count');
  const failedMetricCount = nonNegativeInteger(
    value.failed_metric_count,
    'failed metric count',
  );
  const actualFailed = metricResults.filter(
    (item) => item.outcome === 'fail',
  ).length;
  if (
    metricCount !== metricResults.length
    || failedMetricCount !== actualFailed
    || (
      value.state === 'passed'
        ? actualFailed !== 0
        : actualFailed === 0
    )
  ) {
    throw new Error(
      'Semantic guideline assessment counts contradict its metric evidence.',
    );
  }
  return {
    projection: 'detail',
    receipt_id: requiredText(value.receipt_id, 'receipt identity'),
    board_id: expected.boardId,
    entity_type: expected.entityType,
    subject_id: expected.subjectId,
    subject_version: value.subject_version as number,
    validation_edition: validationEdition,
    binding_id: requiredText(value.binding_id, 'binding identity'),
    guideline_id: requiredText(value.guideline_id, 'guideline identity'),
    guideline_revision_id: requiredText(
      value.guideline_revision_id,
      'guideline revision identity',
    ),
    enforcement: value.enforcement,
    state: value.state,
    currentness: resolvedCurrentness.currentness,
    currentness_reasons: resolvedCurrentness.reasons,
    confidence,
    minimum_confidence: minimumConfidence,
    metric_count: metricCount,
    failed_metric_count: failedMetricCount,
    recorded_at: timestamp(value.recorded_at, 'recorded timestamp'),
    binding_revision: positiveInteger(
      value.binding_revision,
      'binding revision',
    ),
    assessor_agent_id: requiredText(
      value.assessor_agent_id,
      'assessor identity',
    ),
    assessor_model_id: nullableText(
      value.assessor_model_id,
      'assessor model identity',
    ),
    assessor_independent: value.assessor_independent,
    confidence_admissible: value.confidence_admissible,
    metric_results: metricResults,
  };
}

export function parseSemanticFindingDetail(
  value: SemanticFindingListItem | unknown,
  expected: SemanticSubjectExpectation,
): SemanticFindingDetail {
  if (!isRecord(value)) {
    throw new Error('Semantic guideline finding is invalid.');
  }
  exactFields(value, FINDING_DETAIL_FIELDS, 'finding');
  if (value.projection !== 'detail') {
    throw new Error('Semantic guideline finding projection is not detail.');
  }
  matchesSubject(value, expected);
  const resolvedCurrentness = currentness(value);
  if (!Array.isArray(value.evidence_refs) || value.evidence_refs.length === 0) {
    throw new Error('Semantic guideline finding evidence is missing.');
  }
  if (!Array.isArray(value.pinpoints) || value.pinpoints.length === 0) {
    throw new Error('Semantic guideline finding pinpoints are missing.');
  }
  return {
    projection: 'detail',
    finding_id: requiredText(value.finding_id, 'finding identity'),
    receipt_id: requiredText(value.receipt_id, 'finding receipt identity'),
    board_id: expected.boardId,
    entity_type: expected.entityType,
    subject_id: expected.subjectId,
    subject_version: value.subject_version as number,
    guideline_id: requiredText(value.guideline_id, 'finding guideline'),
    guideline_revision_id: requiredText(
      value.guideline_revision_id,
      'finding guideline revision',
    ),
    binding_id: requiredText(value.binding_id, 'finding binding'),
    metric_id: requiredText(value.metric_id, 'finding metric'),
    metric_code: metricCode(value.metric_code, 'finding metric code'),
    currentness: resolvedCurrentness.currentness,
    currentness_reasons: resolvedCurrentness.reasons,
    created_at: timestamp(value.created_at, 'finding timestamp'),
    metric_result_id: requiredText(
      value.metric_result_id,
      'finding metric result',
    ),
    binding_revision: positiveInteger(
      value.binding_revision,
      'finding binding revision',
    ),
    rationale: requiredText(value.rationale, 'finding rationale'),
    evidence_refs: uniqueBy(
      value.evidence_refs.map(parseEvidence),
      (item) =>
        `${item.source_type}:${item.source_id}:${item.source_version}:${item.content_hash}`,
      'finding evidence references',
    ),
    pinpoints: uniqueBy(
      value.pinpoints.map(parsePinpoint),
      (item) =>
        `${item.anchor_type}:${item.anchor_ref ?? ''}:${item.excerpt_hash ?? ''}:${item.input_digest}`,
      'finding pinpoints',
    ),
  };
}

export function parseSemanticWaiverDetail(
  value: SemanticWaiverListItem | unknown,
  expected: SemanticSubjectExpectation,
): SemanticWaiverDetail {
  if (!isRecord(value)) {
    throw new Error('Semantic guideline waiver is invalid.');
  }
  exactFields(value, WAIVER_DETAIL_FIELDS, 'waiver');
  if (value.projection !== 'detail') {
    throw new Error('Semantic guideline waiver projection is not detail.');
  }
  matchesSubject(value, expected);
  const statuses = new Set([
    'requested',
    'approved',
    'rejected',
    'revoked',
    'expired',
  ]);
  const events = new Set([
    'request',
    'approve',
    'reject',
    'revoke',
    'expire',
    'revalidate',
  ]);
  if (!statuses.has(String(value.status)) || !events.has(String(value.last_event_type))) {
    throw new Error('Semantic guideline waiver lifecycle is invalid.');
  }
  const resolvedCurrentness = currentness(value);
  if (!Array.isArray(value.evidence_refs) || value.evidence_refs.length === 0) {
    throw new Error('Semantic guideline waiver evidence is missing.');
  }
  const expireReasons = new Set([
    'scheduled_expiry',
    'subject_scope_changed',
    'guideline_revision_changed',
    'binding_configuration_changed',
    'metric_result_changed',
  ]);
  if (
    value.expire_reason !== null
    && !expireReasons.has(String(value.expire_reason))
  ) {
    throw new Error('Semantic guideline waiver expiry reason is invalid.');
  }
  const requestedAt = timestamp(value.requested_at, 'waiver request timestamp');
  const lastEventAt = timestamp(value.last_event_at, 'waiver event timestamp');
  if (Date.parse(lastEventAt) < Date.parse(requestedAt)) {
    throw new Error('Semantic guideline waiver event time regressed.');
  }
  const reviewedBy = nullableText(value.reviewed_by, 'waiver reviewer');
  const reviewedAt = nullableTimestamp(
    value.reviewed_at,
    'waiver review timestamp',
  );
  const reviewReason = nullableText(
    value.review_reason,
    'waiver review reason',
  );
  const revokedBy = nullableText(value.revoked_by, 'waiver revoker');
  const revokedAt = nullableTimestamp(
    value.revoked_at,
    'waiver revocation timestamp',
  );
  if (
    (reviewedBy === null) !== (reviewedAt === null)
    || (reviewedBy === null) !== (reviewReason === null)
    || (revokedBy === null) !== (revokedAt === null)
  ) {
    throw new Error('Semantic guideline waiver lifecycle evidence is partial.');
  }
  return {
    projection: 'detail',
    waiver_id: requiredText(value.waiver_id, 'waiver identity'),
    board_id: expected.boardId,
    entity_type: expected.entityType,
    subject_id: expected.subjectId,
    subject_version: value.subject_version as number,
    finding_id: requiredText(value.finding_id, 'waiver finding identity'),
    receipt_id: requiredText(value.receipt_id, 'waiver receipt identity'),
    guideline_id: requiredText(value.guideline_id, 'waiver guideline identity'),
    guideline_revision_id: requiredText(
      value.guideline_revision_id,
      'waiver guideline revision identity',
    ),
    binding_id: requiredText(value.binding_id, 'waiver binding identity'),
    metric_id: requiredText(value.metric_id, 'waiver metric identity'),
    metric_code: metricCode(value.metric_code, 'waiver metric code'),
    status: value.status as SemanticWaiverDetail['status'],
    currentness: resolvedCurrentness.currentness,
    currentness_reasons: resolvedCurrentness.reasons,
    waiver_revision: positiveInteger(
      value.waiver_revision,
      'waiver revision',
    ),
    requested_at: requestedAt,
    expires_at: nullableTimestamp(value.expires_at, 'waiver expiry timestamp'),
    last_event_type:
      value.last_event_type as SemanticWaiverDetail['last_event_type'],
    last_event_at: lastEventAt,
    justification: requiredText(value.justification, 'waiver justification'),
    requested_by: requiredText(value.requested_by, 'waiver requester'),
    original_expires_at: nullableTimestamp(
      value.original_expires_at,
      'waiver original expiry timestamp',
    ),
    reviewed_by: reviewedBy,
    reviewed_at: reviewedAt,
    review_reason: reviewReason,
    revoked_by: revokedBy,
    revoked_at: revokedAt,
    expire_reason:
      value.expire_reason as SemanticWaiverDetail['expire_reason'],
    evidence_refs: uniqueBy(
      value.evidence_refs.map(parseEvidence),
      (item) =>
        `${item.source_type}:${item.source_id}:${item.source_version}:${item.content_hash}`,
      'waiver evidence references',
    ),
  };
}

export function parseSemanticSkipDetail(
  value: SemanticSkipListItem | unknown,
  expected: SemanticSubjectExpectation,
): SemanticSkipDetail {
  if (!isRecord(value)) {
    throw new Error('Semantic guideline skip is invalid.');
  }
  exactFields(value, SKIP_DETAIL_FIELDS, 'skip');
  if (value.projection !== 'detail') {
    throw new Error('Semantic guideline skip projection is not detail.');
  }
  matchesSubject(value, expected);
  if (
    (value.status !== 'active' && value.status !== 'revoked')
    || (value.last_event_type !== 'create' && value.last_event_type !== 'revoke')
  ) {
    throw new Error('Semantic guideline skip lifecycle is invalid.');
  }
  const resolvedCurrentness = currentness(value);
  const createdAt = timestamp(value.created_at, 'skip creation timestamp');
  const lastEventAt = timestamp(value.last_event_at, 'skip event timestamp');
  if (Date.parse(lastEventAt) < Date.parse(createdAt)) {
    throw new Error('Semantic guideline skip event time regressed.');
  }
  const revokedBy = nullableText(value.revoked_by, 'skip revoker');
  const revokedAt = nullableTimestamp(
    value.revoked_at,
    'skip revocation timestamp',
  );
  const revocationReason = nullableText(
    value.revocation_reason,
    'skip revocation reason',
  );
  if (
    value.status === 'active'
      ? (
          value.last_event_type !== 'create'
          || revokedBy !== null
          || revokedAt !== null
          || revocationReason !== null
        )
      : (
          value.last_event_type !== 'revoke'
          || revokedBy === null
          || revokedAt === null
          || revocationReason === null
        )
  ) {
    throw new Error('Semantic guideline skip lifecycle evidence is inconsistent.');
  }
  return {
    projection: 'detail',
    skip_id: requiredText(value.skip_id, 'skip identity'),
    board_id: expected.boardId,
    entity_type: expected.entityType,
    subject_id: expected.subjectId,
    subject_version: value.subject_version as number,
    guideline_id: requiredText(value.guideline_id, 'skip guideline identity'),
    guideline_revision_id: requiredText(
      value.guideline_revision_id,
      'skip guideline revision identity',
    ),
    binding_id: requiredText(value.binding_id, 'skip binding identity'),
    status: value.status,
    skip_revision: positiveInteger(value.skip_revision, 'skip revision'),
    currentness: resolvedCurrentness.currentness,
    currentness_reasons: resolvedCurrentness.reasons,
    created_at: createdAt,
    last_event_type: value.last_event_type,
    last_event_at: lastEventAt,
    binding_revision: positiveInteger(
      value.binding_revision,
      'skip binding revision',
    ),
    reason: requiredText(value.reason, 'skip reason'),
    created_by: requiredText(value.created_by, 'skip creator'),
    revoked_by: revokedBy,
    revoked_at: revokedAt,
    revocation_reason: revocationReason,
  };
}

export function parseSemanticDetailPage<T>(
  value: SemanticCursorPage<unknown> | unknown,
  parseItem: (item: unknown) => T,
  pageLimit: number,
): {
  items: T[];
  limit: number;
  has_more: boolean;
  next_cursor?: string;
} {
  if (!isRecord(value)) {
    throw new Error('Semantic guideline cursor page is invalid.');
  }
  exactFields(value, PAGE_FIELDS, 'cursor page');
  if (value.projection !== 'detail') {
    throw new Error('Semantic guideline cursor projection is not detail.');
  }
  if (
    !Array.isArray(value.items)
    || typeof value.has_more !== 'boolean'
    || value.items.length > pageLimit
  ) {
    throw new Error('Semantic guideline cursor page is malformed.');
  }
  const nextCursor = value.next_cursor;
  if (
    (value.has_more && (
      typeof nextCursor !== 'string'
      || nextCursor.length === 0
      || value.items.length === 0
    ))
    || (!value.has_more && nextCursor !== null)
  ) {
    throw new Error('Semantic guideline cursor continuation is invalid.');
  }
  const items = value.items.map(parseItem);
  return value.has_more
    ? {
        items,
        limit: pageLimit,
        has_more: true,
        next_cursor: nextCursor as string,
      }
    : {
        items,
        limit: pageLimit,
        has_more: false,
      };
}

export function semanticMetricDirection(
  direction: 'minimum' | 'maximum',
): 'higher-is-better' | 'lower-is-better' {
  return direction === 'minimum'
    ? 'higher-is-better'
    : 'lower-is-better';
}

export function semanticProjectionMatches(
  item: SemanticAssessmentListItem,
  projection: PolicyProjection,
): boolean {
  return item.projection === projection;
}

export function parseRequestedSemanticWaiverResponse(
  value: unknown,
): RequestedSemanticWaiverResponse {
  if (!isRecord(value)) {
    throw new Error('Semantic guideline waiver mutation is invalid.');
  }
  exactFields(value, REQUESTED_WAIVER_FIELDS, 'waiver mutation');
  if (value.status !== 'requested') {
    throw new Error('Semantic guideline waiver mutation status is invalid.');
  }
  return {
    waiver_id: requiredText(value.waiver_id, 'waiver mutation identity'),
    status: 'requested',
    scope_digest: sha256(value.scope_digest, 'waiver scope digest'),
  };
}

export function parseCreatedSemanticSkipResponse(
  value: unknown,
): CreatedSemanticSkipResponse {
  if (!isRecord(value)) {
    throw new Error('Semantic guideline skip creation is invalid.');
  }
  exactFields(value, CREATED_SKIP_FIELDS, 'skip creation');
  return {
    skip_id: requiredText(value.skip_id, 'skip creation identity'),
    scope_digest: sha256(value.scope_digest, 'skip scope digest'),
    created_by: requiredText(value.created_by, 'skip creator'),
  };
}

export function parseRevokedSemanticSkipResponse(
  value: unknown,
): RevokedSemanticSkipResponse {
  if (!isRecord(value)) {
    throw new Error('Semantic guideline skip revocation is invalid.');
  }
  exactFields(value, REVOKED_SKIP_FIELDS, 'skip revocation');
  if (
    value.status !== 'revoked'
    || typeof value.replayed !== 'boolean'
  ) {
    throw new Error('Semantic guideline skip revocation state is invalid.');
  }
  return {
    skip_id: requiredText(value.skip_id, 'skip revocation identity'),
    skip_revision: positiveInteger(
      value.skip_revision,
      'skip revocation revision',
    ),
    status: 'revoked',
    revoked_by: requiredText(value.revoked_by, 'skip revoker'),
    replayed: value.replayed,
  };
}

const CURRENT_RESPONSE_FIELDS = ['contract_version', 'assessment'] as const;
const V2_ASSESSMENT_FIELDS = [
  'receipt_id',
  'receipt_digest',
  'currentness',
  'board_id',
  'subject_type',
  'subject_id',
  'subject_version',
  'validation_edition',
  'binding_id',
  'guideline_id',
  'guideline_revision_id',
  'confidence',
  'recorded_at',
  'metrics',
] as const;
const V2_METRIC_FIELDS = [
  'metric_result_id',
  'metric_result_digest',
  'metric_id',
  'metric_code',
  'score',
  'direction',
  'default_threshold',
  'effective_threshold',
  'threshold_source',
  'outcome',
  'blocking',
  'pinpoints',
] as const;
const V2_PINPOINT_FIELDS = [
  'contract_version',
  'pinpoint_key',
  'kind',
  'title',
  'detail',
  'severity',
  'remediation',
  'anchor',
  'anchor_snapshot',
  'blocking',
] as const;
const V2_ANCHOR_FIELDS = [
  'anchor_type',
  'anchor_ref',
  'excerpt_hash',
] as const;
const V2_SNAPSHOT_FIELDS = [
  'label',
  'excerpt',
  'source_version',
  'availability_at_seal',
] as const;

function booleanValue(value: unknown, label: string): boolean {
  if (typeof value !== 'boolean') {
    throw new Error(`Semantic guideline ${label} is invalid.`);
  }
  return value;
}

function parseV2Anchor(value: unknown): SemanticAnchorV2 {
  if (!isRecord(value)) {
    throw new Error('Semantic guideline v2 anchor is invalid.');
  }
  exactFields(value, V2_ANCHOR_FIELDS, 'v2 anchor');
  if (
    value.anchor_type !== 'whole_artifact'
    && value.anchor_type !== 'field'
    && value.anchor_type !== 'structured_child'
    && value.anchor_type !== 'qa'
  ) {
    throw new Error('Semantic guideline v2 anchor type is invalid.');
  }
  const anchorRef = nullableText(value.anchor_ref, 'v2 anchor reference');
  if (
    (value.anchor_type === 'whole_artifact' && anchorRef !== null)
    || (value.anchor_type !== 'whole_artifact' && anchorRef === null)
  ) {
    throw new Error('Semantic guideline v2 anchor shape is invalid.');
  }
  return {
    anchor_type: value.anchor_type,
    anchor_ref: anchorRef,
    excerpt_hash: nullableSha256(value.excerpt_hash, 'v2 excerpt hash'),
  };
}

function parseV2Pinpoint(
  value: unknown,
  outcome: SemanticMetricOutcome,
): SemanticPinpointV2 {
  if (!isRecord(value)) {
    throw new Error('Semantic guideline v2 pinpoint is invalid.');
  }
  exactFields(value, V2_PINPOINT_FIELDS, 'v2 pinpoint');
  if (value.contract_version !== 'v2') {
    throw new Error('Semantic guideline v2 pinpoint version is invalid.');
  }
  if (value.kind !== 'evidence' && value.kind !== 'issue') {
    throw new Error('Semantic guideline v2 pinpoint kind is invalid.');
  }
  if (
    value.severity !== null
    && value.severity !== 'low'
    && value.severity !== 'medium'
    && value.severity !== 'high'
    && value.severity !== 'critical'
  ) {
    throw new Error('Semantic guideline v2 pinpoint severity is invalid.');
  }
  if (value.kind === 'issue' && value.severity === null) {
    throw new Error('Semantic guideline v2 issue severity is missing.');
  }
  const anchorSnapshot = value.anchor_snapshot;
  if (!isRecord(anchorSnapshot)) {
    throw new Error('Semantic guideline v2 anchor snapshot is invalid.');
  }
  exactFields(anchorSnapshot, V2_SNAPSHOT_FIELDS, 'v2 anchor snapshot');
  if (
    anchorSnapshot.availability_at_seal !== 'available'
    && anchorSnapshot.availability_at_seal !== 'removed'
    && anchorSnapshot.availability_at_seal !== 'inaccessible'
  ) {
    throw new Error('Semantic guideline v2 snapshot availability is invalid.');
  }
  const excerpt = nullableText(
    anchorSnapshot.excerpt,
    'v2 snapshot excerpt',
  );
  if (
    anchorSnapshot.availability_at_seal === 'inaccessible'
    && excerpt !== null
  ) {
    throw new Error('Semantic guideline v2 inaccessible snapshot leaks text.');
  }
  const blocking = booleanValue(value.blocking, 'v2 pinpoint blocking state');
  if (blocking !== (value.kind === 'issue' && outcome === 'fail')) {
    throw new Error('Semantic guideline v2 pinpoint blocking state is inconsistent.');
  }
  return {
    contract_version: 'v2',
    pinpoint_key: requiredText(value.pinpoint_key, 'v2 pinpoint key'),
    kind: value.kind,
    title: requiredText(value.title, 'v2 pinpoint title'),
    detail: requiredText(value.detail, 'v2 pinpoint detail'),
    severity: value.severity,
    remediation: nullableText(value.remediation, 'v2 pinpoint remediation'),
    anchor: parseV2Anchor(value.anchor),
    anchor_snapshot: {
      label: requiredText(anchorSnapshot.label, 'v2 snapshot label'),
      excerpt,
      source_version: requiredText(
        anchorSnapshot.source_version,
        'v2 snapshot source version',
      ),
      availability_at_seal: anchorSnapshot.availability_at_seal,
    },
    blocking,
  };
}

function parseV2Metric(value: unknown): SemanticMetricResultV2 {
  if (!isRecord(value)) {
    throw new Error('Semantic guideline v2 metric is invalid.');
  }
  exactFields(value, V2_METRIC_FIELDS, 'v2 metric');
  if (value.direction !== 'minimum' && value.direction !== 'maximum') {
    throw new Error('Semantic guideline v2 metric direction is invalid.');
  }
  if (
    value.threshold_source !== 'default'
    && value.threshold_source !== 'override'
  ) {
    throw new Error('Semantic guideline v2 threshold source is invalid.');
  }
  if (value.outcome !== 'pass' && value.outcome !== 'fail') {
    throw new Error('Semantic guideline v2 metric outcome is invalid.');
  }
  const score = boundedScore(value.score, 'v2 metric score');
  const defaultThreshold = boundedScore(
    value.default_threshold,
    'v2 default threshold',
  );
  const effectiveThreshold = boundedScore(
    value.effective_threshold,
    'v2 effective threshold',
  );
  if (
    value.threshold_source === 'default'
    && defaultThreshold !== effectiveThreshold
  ) {
    throw new Error('Semantic guideline v2 threshold source is inconsistent.');
  }
  const computedOutcome: SemanticMetricOutcome = value.direction === 'minimum'
    ? score >= effectiveThreshold ? 'pass' : 'fail'
    : score <= effectiveThreshold ? 'pass' : 'fail';
  if (computedOutcome !== value.outcome) {
    throw new Error('Semantic guideline v2 metric outcome is inconsistent.');
  }
  if (!Array.isArray(value.pinpoints) || value.pinpoints.length === 0) {
    throw new Error('Semantic guideline v2 metric pinpoints are missing.');
  }
  const pinpoints = uniqueBy(
    value.pinpoints.map((item) => parseV2Pinpoint(item, value.outcome as SemanticMetricOutcome)),
    (item) => item.pinpoint_key,
    'v2 pinpoint keys',
  );
  const blocking = booleanValue(value.blocking, 'v2 metric blocking state');
  if (blocking !== pinpoints.some((item) => item.blocking)) {
    throw new Error('Semantic guideline v2 metric blocking state is inconsistent.');
  }
  return {
    metric_result_id: requiredText(value.metric_result_id, 'v2 metric result identity'),
    metric_result_digest: sha256(value.metric_result_digest, 'v2 metric result digest'),
    metric_id: requiredText(value.metric_id, 'v2 metric identity'),
    metric_code: metricCode(value.metric_code, 'v2 metric code'),
    score,
    direction: value.direction,
    default_threshold: defaultThreshold,
    effective_threshold: effectiveThreshold,
    threshold_source: value.threshold_source,
    outcome: value.outcome,
    blocking,
    pinpoints,
  };
}

function parseV2Assessment(
  value: unknown,
  expected: SemanticSubjectExpectation,
): SemanticAssessmentCurrentV2 {
  if (!isRecord(value)) {
    throw new Error('Semantic guideline v2 assessment is invalid.');
  }
  exactFields(value, V2_ASSESSMENT_FIELDS, 'v2 assessment');
  if (value.currentness !== 'current') {
    throw new Error('Semantic guideline v2 currentness is invalid.');
  }
  if (
    value.board_id !== expected.boardId
    || value.subject_type !== expected.entityType
    || value.subject_id !== expected.subjectId
  ) {
    throw new Error('Semantic guideline v2 evidence does not match the active subject.');
  }
  if (!Array.isArray(value.metrics) || value.metrics.length === 0) {
    throw new Error('Semantic guideline v2 metrics are missing.');
  }
  const metrics = uniqueBy(
    value.metrics.map(parseV2Metric),
    (item) => `${item.metric_id}:${item.metric_result_id}:${item.metric_code}`,
    'v2 metric results',
  );
  const validationEdition = value.validation_edition === null
    ? null
    : positiveInteger(value.validation_edition, 'v2 validation edition');
  if (
    expected.validationEdition !== undefined
    && validationEdition !== expected.validationEdition
  ) {
    throw new Error(
      'Semantic guideline v2 assessment does not match the active validation edition.',
    );
  }
  return {
    receipt_id: requiredText(value.receipt_id, 'v2 receipt identity'),
    receipt_digest: sha256(value.receipt_digest, 'v2 receipt digest'),
    currentness: 'current',
    board_id: expected.boardId,
    subject_type: expected.entityType,
    subject_id: expected.subjectId,
    subject_version: positiveInteger(value.subject_version, 'v2 subject version'),
    validation_edition: validationEdition,
    binding_id: requiredText(value.binding_id, 'v2 binding identity'),
    guideline_id: requiredText(value.guideline_id, 'v2 guideline identity'),
    guideline_revision_id: requiredText(
      value.guideline_revision_id,
      'v2 guideline revision identity',
    ),
    confidence: boundedScore(value.confidence, 'v2 confidence'),
    recorded_at: timestamp(value.recorded_at, 'v2 recorded timestamp'),
    metrics,
  };
}

/**
 * Closed discriminator for the current-assessment REST envelope. Unknown keys
 * or versions fail safely instead of being interpreted as a legacy receipt.
 */
export function parseCurrentSemanticAssessmentResponse(
  value: unknown,
  expected: SemanticSubjectExpectation,
): SemanticCurrentAssessmentResponse {
  if (!isRecord(value)) {
    throw new Error('Semantic guideline current response is invalid.');
  }
  exactFields(value, CURRENT_RESPONSE_FIELDS, 'current response');
  if (value.contract_version === 'v1') {
    return {
      contract_version: 'v1',
      assessment: parseSemanticAssessmentDetail(value.assessment, expected),
    };
  }
  if (value.contract_version === 'v2') {
    return {
      contract_version: 'v2',
      assessment: parseV2Assessment(value.assessment, expected),
    };
  }
  throw new Error('Semantic guideline contract version is unsupported.');
}

export type SemanticPolicyUiState =
  | 'fail'
  | 'positive_evidence'
  | 'non_blocking_warning'
  | 'waived_fail_finding'
  | 'stale'
  | 'legacy'
  | 'removed'
  | 'inaccessible'
  | 'loading'
  | 'no_assessment'
  | 'no_visible_pinpoints'
  | 'recoverable_transport_error';

export type SemanticAnchorResolution =
  | { state: 'available'; navigationTarget: string }
  | { state: 'removed' }
  | { state: 'inaccessible' };

export interface SemanticPinpointTechnicalDetails {
  anchorType: SemanticPinpoint['anchor_type'];
  sourceVersion?: string;
  anchorReference?: string;
  excerptHash?: string;
  inputDigest?: string;
  metricResultDigest?: string;
}

export interface SemanticPinpointViewModel {
  contractVersion: 'v1' | 'v2';
  state: 'available' | 'removed' | 'inaccessible' | 'legacy';
  kind: 'evidence' | 'issue' | 'legacy';
  title: string;
  detail: string;
  severity: SemanticPinpointV2['severity'];
  remediation: string | null;
  blocking: boolean;
  categoryLabel: string;
  locationLabel: string;
  excerpt: string | null;
  navigationTarget: string | null;
  unavailableMessage: string | null;
  technicalDetails: SemanticPinpointTechnicalDetails | null;
}

function anchorCategoryLabel(
  anchorType: SemanticPinpoint['anchor_type'],
): string {
  switch (anchorType) {
    case 'whole_artifact':
      return 'Whole artifact';
    case 'field':
      return 'Field';
    case 'structured_child':
      return 'Structured item';
    case 'qa':
      return 'Q&A';
  }
}

export interface SemanticPolicyMetricViewModel {
  metricCode: string;
  score: number;
  direction: SemanticMetricResultDetail['direction'];
  effectiveThreshold: number;
  outcome: SemanticMetricOutcome;
  uiState: SemanticPolicyUiState;
  rationale: string | null;
  pinpoints: SemanticPinpointViewModel[];
}

export interface SemanticPolicyViewModel {
  contractVersion: 'v1' | 'v2';
  currentness: PolicyCurrentness;
  uiStates: SemanticPolicyUiState[];
  confidence: number;
  recordedAt: string;
  metrics: SemanticPolicyMetricViewModel[];
}

export interface SemanticPolicyResolverOptions {
  resolveAnchor?: (
    anchor: SemanticAnchorV2 | SemanticPinpoint,
  ) => SemanticAnchorResolution;
  canViewTechnicalDetails?: boolean;
  waivedMetricCodes?: ReadonlySet<string>;
}

const OPAQUE_ID = /^(?:[0-9a-f]{8}-[0-9a-f-]{27,}|[0-9a-f]{32,})$/iu;

function bestEffortLegacyLabel(pinpoint: SemanticPinpoint): string {
  if (pinpoint.anchor_type === 'whole_artifact') return 'Whole artifact';
  if (pinpoint.anchor_type === 'qa') return 'Question or answer';
  if (!pinpoint.anchor_ref || OPAQUE_ID.test(pinpoint.anchor_ref)) {
    return pinpoint.anchor_type === 'field'
      ? 'Referenced field'
      : 'Referenced item';
  }
  return pinpoint.anchor_ref
    .replace(/[_-]+/gu, ' ')
    .replace(/\b\p{L}/gu, (letter) => letter.toUpperCase());
}

function safeResolution(
  resolveAnchor: SemanticPolicyResolverOptions['resolveAnchor'],
  anchor: SemanticAnchorV2 | SemanticPinpoint,
): SemanticAnchorResolution {
  const resolution = resolveAnchor?.(anchor) ?? { state: 'inaccessible' };
  if (
    resolution.state === 'available'
    && resolution.navigationTarget.trim() === ''
  ) {
    throw new Error('Semantic guideline navigation target is invalid.');
  }
  return resolution;
}

function v2PinpointView(
  pinpoint: SemanticPinpointV2,
  metric: SemanticMetricResultV2,
  options: SemanticPolicyResolverOptions,
): SemanticPinpointViewModel {
  const resolution = safeResolution(options.resolveAnchor, pinpoint.anchor);
  const inaccessible = resolution.state === 'inaccessible';
  const removed = resolution.state === 'removed';
  return {
    contractVersion: 'v2',
    state: resolution.state,
    kind: pinpoint.kind,
    title: pinpoint.title,
    detail: pinpoint.detail,
    severity: pinpoint.severity,
    remediation: pinpoint.remediation,
    blocking: pinpoint.blocking,
    categoryLabel: anchorCategoryLabel(pinpoint.anchor.anchor_type),
    locationLabel: inaccessible
      ? 'Restricted assessment location'
      : pinpoint.anchor_snapshot.label,
    excerpt: inaccessible ? null : pinpoint.anchor_snapshot.excerpt,
    navigationTarget: resolution.state === 'available'
      ? resolution.navigationTarget
      : null,
    unavailableMessage: removed
      ? 'Referenced element is no longer available.'
      : inaccessible
        ? 'Location unavailable with your current access.'
        : null,
    technicalDetails: options.canViewTechnicalDetails
      ? {
          anchorType: pinpoint.anchor.anchor_type,
          sourceVersion: pinpoint.anchor_snapshot.source_version,
          ...(!inaccessible && pinpoint.anchor.anchor_ref
            ? { anchorReference: pinpoint.anchor.anchor_ref }
            : {}),
          ...(!inaccessible && pinpoint.anchor.excerpt_hash
            ? { excerptHash: pinpoint.anchor.excerpt_hash }
            : {}),
          ...(!inaccessible
            ? { metricResultDigest: metric.metric_result_digest }
            : {}),
        }
      : null,
  };
}

function legacyPinpointView(
  pinpoint: SemanticPinpoint,
  rationale: string,
  outcome: SemanticMetricOutcome,
  options: SemanticPolicyResolverOptions,
): SemanticPinpointViewModel {
  const resolution = safeResolution(options.resolveAnchor, pinpoint);
  const inaccessible = resolution.state === 'inaccessible';
  const removed = resolution.state === 'removed';
  return {
    contractVersion: 'v1',
    state: inaccessible || removed ? resolution.state : 'legacy',
    kind: 'legacy',
    title: 'Legacy assessment evidence',
    detail: rationale,
    severity: null,
    remediation: null,
    blocking: outcome === 'fail',
    categoryLabel: anchorCategoryLabel(pinpoint.anchor_type),
    locationLabel: inaccessible
      ? 'Restricted assessment location'
      : bestEffortLegacyLabel(pinpoint),
    excerpt: null,
    navigationTarget: resolution.state === 'available'
      ? resolution.navigationTarget
      : null,
    unavailableMessage: removed
      ? 'Referenced element is no longer available.'
      : inaccessible
        ? 'Location unavailable with your current access.'
        : null,
    technicalDetails: options.canViewTechnicalDetails
      ? {
          anchorType: pinpoint.anchor_type,
          ...(!inaccessible && pinpoint.anchor_ref
            ? { anchorReference: pinpoint.anchor_ref }
            : {}),
          ...(!inaccessible && pinpoint.excerpt_hash
            ? { excerptHash: pinpoint.excerpt_hash }
            : {}),
          ...(!inaccessible ? { inputDigest: pinpoint.input_digest } : {}),
        }
      : null,
  };
}

function uniqueStates(states: SemanticPolicyUiState[]): SemanticPolicyUiState[] {
  return [...new Set(states)];
}

/** Build the presentation-safe model; sealed text explains, live access navigates. */
export function resolveSemanticPolicyViewModel(
  response: SemanticCurrentAssessmentResponse,
  options: SemanticPolicyResolverOptions = {},
): SemanticPolicyViewModel {
  if (response.contract_version === 'v1') {
    const metrics = response.assessment.metric_results.map((metric) => ({
      metricCode: metric.metric_code,
      score: metric.score,
      direction: metric.direction,
      effectiveThreshold: metric.effective_threshold,
      outcome: metric.outcome,
      uiState: response.assessment.currentness === 'stale'
        ? 'stale' as const
        : 'legacy' as const,
      rationale: metric.rationale,
      pinpoints: metric.pinpoints.map((pinpoint) => legacyPinpointView(
        pinpoint,
        metric.rationale,
        metric.outcome,
        options,
      )),
    }));
    return {
      contractVersion: 'v1',
      currentness: response.assessment.currentness,
      uiStates: uniqueStates([
        response.assessment.currentness === 'stale' ? 'stale' : 'legacy',
        ...metrics.flatMap((metric) => metric.pinpoints
          .filter((pinpoint) => pinpoint.state !== 'legacy')
          .map((pinpoint) => pinpoint.state as 'removed' | 'inaccessible')),
      ]),
      confidence: response.assessment.confidence,
      recordedAt: response.assessment.recorded_at,
      metrics,
    };
  }

  const metrics = response.assessment.metrics.map((metric) => {
    const waived = options.waivedMetricCodes?.has(metric.metric_code) ?? false;
    const uiState: SemanticPolicyUiState = metric.outcome === 'fail'
      ? waived ? 'waived_fail_finding' : 'fail'
      : metric.pinpoints.some((pinpoint) => pinpoint.kind === 'issue')
        ? 'non_blocking_warning'
        : 'positive_evidence';
    return {
      metricCode: metric.metric_code,
      score: metric.score,
      direction: metric.direction,
      effectiveThreshold: metric.effective_threshold,
      outcome: metric.outcome,
      uiState,
      rationale: null,
      pinpoints: metric.pinpoints.map((pinpoint) =>
        v2PinpointView(pinpoint, metric, options)
      ),
    };
  });
  return {
    contractVersion: 'v2',
    currentness: 'current',
    uiStates: uniqueStates([
      ...metrics.map((metric) => metric.uiState),
      ...metrics.flatMap((metric) => metric.pinpoints
        .filter((pinpoint) => pinpoint.state !== 'available')
        .map((pinpoint) => pinpoint.state as 'removed' | 'inaccessible')),
    ]),
    confidence: response.assessment.confidence,
    recordedAt: response.assessment.recorded_at,
    metrics,
  };
}

export type SemanticPolicyRenderOutcome =
  | 'current'
  | 'stale'
  | 'waived'
  | 'unavailable'
  | 'legacy'
  | 'system_error';

export interface SemanticPolicyRenderTelemetry {
  metric: 'pulse_policy_compliance_render_total';
  labels: {
    contract_version: 'v1' | 'v2' | 'none';
    outcome: SemanticPolicyRenderOutcome;
  };
}

/** Closed, payload-free telemetry labels safe for any frontend sink. */
export function semanticPolicyRenderTelemetry(
  state: SemanticPolicyUiState,
  contractVersion: 'v1' | 'v2' | 'none',
): SemanticPolicyRenderTelemetry {
  const outcome: SemanticPolicyRenderOutcome = state === 'stale'
    ? 'stale'
    : state === 'waived_fail_finding'
      ? 'waived'
      : state === 'legacy'
        ? 'legacy'
        : state === 'removed'
          || state === 'inaccessible'
          || state === 'no_assessment'
          || state === 'no_visible_pinpoints'
          ? 'unavailable'
          : state === 'recoverable_transport_error'
            ? 'system_error'
            : 'current';
  return {
    metric: 'pulse_policy_compliance_render_total',
    labels: { contract_version: contractVersion, outcome },
  };
}
