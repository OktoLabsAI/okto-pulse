import type {
  CreatedSemanticSkipResponse,
  PolicyCurrentness,
  PolicyEntityType,
  PolicyProjection,
  SemanticAssessmentCurrentnessReason,
  SemanticAssessmentDetail,
  SemanticAssessmentListItem,
  SemanticCursorPage,
  SemanticEvidenceRef,
  SemanticFindingDetail,
  SemanticFindingListItem,
  SemanticMetricOutcome,
  SemanticMetricResultDetail,
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
