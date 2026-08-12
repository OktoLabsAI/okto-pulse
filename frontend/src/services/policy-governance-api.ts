/**
 * Authenticated REST client for SK-B guideline policy governance.
 *
 * The legacy dashboard API keeps listGuidelines unchanged until the policy UI
 * migrates atomically. This client exposes only the new board-scoped,
 * revisioned policy surface and never derives SemVer, currentness, subject
 * versions, digests, or gate decisions locally.
 */

import { useMemo } from 'react';

import { useApiClient } from '@/contexts/ApiContext';
import type * as Policy from '@/types/policy-governance';

const BOARDS_ROOT = '/boards';
const POLICY_PAGE_LIMIT_DEFAULT = 50;
const POLICY_PAGE_LIMIT_MAX = 200;

export interface PolicyGovernanceTransport {
  fetch(url: string, options?: RequestInit): Promise<Response>;
}

export class PolicyGovernanceApiError extends Error {
  readonly status: number;
  readonly kind: Policy.PolicyErrorKind | null;
  readonly code: string;
  readonly category: Policy.PolicyErrorCategory | null;
  readonly statusCategory: string | null;
  readonly httpStatus: number;
  readonly retryable: boolean;
  readonly nextAction: string | null;
  readonly details: Record<string, string>;

  constructor({
    message,
    status,
    kind = null,
    code,
    category = null,
    statusCategory = null,
    httpStatus = status,
    retryable = false,
    nextAction = null,
    details = {},
  }: {
    message: string;
    status: number;
    kind?: Policy.PolicyErrorKind | null;
    code: string;
    category?: Policy.PolicyErrorCategory | null;
    statusCategory?: string | null;
    httpStatus?: number;
    retryable?: boolean;
    nextAction?: string | null;
    details?: Record<string, string>;
  }) {
    super(message);
    this.name = 'PolicyGovernanceApiError';
    this.status = status;
    this.kind = kind;
    this.code = code;
    this.category = category;
    this.statusCategory = statusCategory;
    this.httpStatus = httpStatus;
    this.retryable = retryable;
    this.nextAction = nextAction;
    this.details = details;
  }
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null
    ? (value as Record<string, unknown>)
    : null;
}

function stringRecord(value: unknown): Record<string, string> {
  const record = asRecord(value);
  if (record === null) return {};
  return Object.fromEntries(
    Object.entries(record).filter(
      (entry): entry is [string, string] => typeof entry[1] === 'string',
    ),
  );
}

const policyErrorKinds = new Set<Policy.PolicyErrorKind>([
  'validation_failed',
  'under_bump',
  'permission_denied',
  'not_found',
  'conflict',
  'service_unavailable',
  'invalid_cursor',
]);

const policyErrorCategories = new Set<Policy.PolicyErrorCategory>([
  'invalid_argument',
  'permission_denied',
  'not_found',
  'conflict',
  'service_unavailable',
]);

function policyErrorKind(value: unknown): Policy.PolicyErrorKind | null {
  return typeof value === 'string'
    && policyErrorKinds.has(value as Policy.PolicyErrorKind)
    ? (value as Policy.PolicyErrorKind)
    : null;
}

function policyErrorCategory(
  value: unknown,
): Policy.PolicyErrorCategory | null {
  return typeof value === 'string'
    && policyErrorCategories.has(value as Policy.PolicyErrorCategory)
    ? (value as Policy.PolicyErrorCategory)
    : null;
}

async function toPolicyApiError(
  response: Response,
): Promise<PolicyGovernanceApiError> {
  const body = asRecord(await response.json().catch(() => null));
  const backendError = asRecord(body?.backend_error);
  const detail =
    asRecord(backendError?.detail)
    ?? asRecord(body?.detail)
    ?? body;
  const kind = policyErrorKind(detail?.error);
  const category = policyErrorCategory(detail?.category);
  const codeValue = detail?.error_code ?? detail?.code ?? detail?.error;
  const code = typeof codeValue === 'string' ? codeValue : 'unexpected_error';
  const message =
    typeof detail?.message === 'string'
      ? detail.message
      : `${code}: HTTP ${response.status}`;

  return new PolicyGovernanceApiError({
    status: response.status,
    kind,
    category,
    code,
    message,
    statusCategory:
      typeof detail?.status_category === 'string'
        ? detail.status_category
        : null,
    httpStatus:
      typeof detail?.http_status === 'number'
        ? detail.http_status
        : response.status,
    retryable: detail?.retryable === true,
    nextAction:
      typeof detail?.next_action === 'string' ? detail.next_action : null,
    details: stringRecord(detail?.details),
  });
}

async function requestJson<T>(
  transport: PolicyGovernanceTransport,
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const headers = new Headers(options.headers);
  if (!headers.has('Accept')) headers.set('Accept', 'application/json');
  if (options.body !== undefined && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }

  const response = await transport.fetch(path, { ...options, headers });
  if (!response.ok) throw await toPolicyApiError(response);
  return (await response.json()) as T;
}

function encoded(value: string): string {
  return encodeURIComponent(value);
}

function boardRoot(boardId: string): string {
  return `${BOARDS_ROOT}/${encoded(boardId)}`;
}

function guidelineRoot(boardId: string, guidelineId: string): string {
  return `${boardRoot(boardId)}/guidelines/${encoded(guidelineId)}`;
}

function waiverRoot(boardId: string, waiverId: string): string {
  return `${boardRoot(boardId)}/policy-waivers/${encoded(waiverId)}`;
}

function requirePageLimit(limit: number | undefined): number {
  const resolved = limit ?? POLICY_PAGE_LIMIT_DEFAULT;
  if (
    !Number.isInteger(resolved)
    || resolved < 1
    || resolved > POLICY_PAGE_LIMIT_MAX
  ) {
    throw new RangeError('policy_page_limit_invalid');
  }
  return resolved;
}

function pageParams(
  options: Policy.PolicyPageOptions = {},
): URLSearchParams {
  const params = new URLSearchParams({
    limit: String(requirePageLimit(options.limit)),
    projection: options.projection ?? 'summary',
  });
  if (options.cursor !== undefined) params.set('cursor', options.cursor);
  return params;
}

function setIfDefined(
  params: URLSearchParams,
  name: string,
  value: string | number | undefined,
): void {
  if (value !== undefined) params.set(name, String(value));
}

function postJson<T>(
  transport: PolicyGovernanceTransport,
  path: string,
  body: unknown,
  signal?: AbortSignal,
): Promise<T> {
  return requestJson<T>(transport, path, {
    method: 'POST',
    body: JSON.stringify(body),
    signal,
  });
}

export interface PolicyGovernanceApi {
  exportGuidelinePolicy(
    boardId: string,
    options?: {
      guidelineIds?: string[];
      includeBindingHistory?: boolean;
      signal?: AbortSignal;
    },
  ): Promise<Policy.GuidelineExportEnvelopeV3>;
  importGuidelinePolicy(
    boardId: string,
    envelope: Policy.GuidelineExportEnvelopeV3,
    options?: { dryRun?: boolean; signal?: AbortSignal },
  ): Promise<Policy.GuidelineImportResult>;
  listGuidelineRevisions(
    boardId: string,
    guidelineId: string,
    options?: Policy.PolicyPageOptions,
  ): Promise<Policy.PolicyCursorPage<Policy.GuidelineRevisionListItem>>;
  createGuidelineRevision(
    boardId: string,
    guidelineId: string,
    request: Policy.CreateGuidelineRevisionRequest,
    signal?: AbortSignal,
  ): Promise<Policy.CreateGuidelineRevisionResponse>;
  getGuidelineRevision(
    boardId: string,
    guidelineId: string,
    revisionId: string,
    signal?: AbortSignal,
  ): Promise<Policy.GuidelineRevisionAuthorityResponse>;
  retireGuideline(
    boardId: string,
    guidelineId: string,
    request: Policy.RetireGuidelineRequest,
    signal?: AbortSignal,
  ): Promise<Policy.RetirementResponse>;
  previewGuidelineImpact(
    boardId: string,
    guidelineId: string,
    request: Policy.PreviewGuidelineImpactRequest,
    signal?: AbortSignal,
  ): Promise<Policy.GuidelineImpactPreviewResponse>;
  getGuidelineImpact(
    boardId: string,
    guidelineId: string,
    previewId: string,
    signal?: AbortSignal,
  ): Promise<Policy.GuidelineImpactPreviewResponse>;
  listGuidelineImpactItems(
    boardId: string,
    guidelineId: string,
    previewId: string,
    options?: Policy.GuidelineImpactItemPageOptions,
  ): Promise<Policy.PolicyCursorPage<Policy.GuidelineImpactPageItem>>;
  adoptGuidelineRevision(
    boardId: string,
    guidelineId: string,
    request: Policy.AdoptGuidelineRevisionRequest,
    signal?: AbortSignal,
  ): Promise<Policy.GuidelineAdoptionResponse>;
  listSemanticGuidelineAssessments(
    boardId: string,
    options?: Policy.SemanticAssessmentPageOptions,
  ): Promise<
    Policy.SemanticCursorPage<Policy.SemanticAssessmentListItem>
  >;
  getCurrentSemanticGuidelineAssessment(
    boardId: string,
    entityType: Policy.PolicyEntityType,
    subjectId: string,
    bindingId: string,
    projection?: Policy.PolicyProjection,
    signal?: AbortSignal,
    validationEdition?: number,
  ): Promise<Policy.SemanticCurrentAssessmentResponse>;
  getSemanticGuidelineAssessment(
    boardId: string,
    receiptId: string,
    projection?: Policy.PolicyProjection,
    signal?: AbortSignal,
  ): Promise<Policy.SemanticAssessmentResponse>;
  listSemanticGuidelineFindings(
    boardId: string,
    options?: Policy.SemanticFindingPageOptions,
  ): Promise<Policy.SemanticCursorPage<Policy.SemanticFindingListItem>>;
  listSemanticMetricWaivers(
    boardId: string,
    options: Policy.SemanticWaiverPageOptions,
  ): Promise<Policy.SemanticCursorPage<Policy.SemanticWaiverListItem>>;
  requestSemanticMetricWaiver(
    boardId: string,
    request: Policy.RequestSemanticMetricWaiverRequest,
    signal?: AbortSignal,
  ): Promise<Policy.RequestedSemanticWaiverResponse>;
  getSemanticMetricWaiver(
    boardId: string,
    waiverId: string,
    options: {
      evaluatedAt: string;
      projection?: Policy.PolicyProjection;
      signal?: AbortSignal;
    },
  ): Promise<Policy.SemanticWaiverResponse>;
  listSemanticMetricWaiverEvents(
    boardId: string,
    waiverId: string,
    signal?: AbortSignal,
  ): Promise<Policy.SemanticWaiverEventsResponse>;
  reviewSemanticMetricWaiver(
    boardId: string,
    waiverId: string,
    request: Policy.ReviewSemanticMetricWaiverRequest,
    signal?: AbortSignal,
  ): Promise<Policy.ReviewedSemanticWaiverResponse>;
  revokeSemanticMetricWaiver(
    boardId: string,
    waiverId: string,
    request: Policy.RevokeSemanticMetricWaiverRequest,
    signal?: AbortSignal,
  ): Promise<Policy.RevokedSemanticWaiverResponse>;
  revalidateSemanticMetricWaiver(
    boardId: string,
    waiverId: string,
    request: Policy.RevalidateSemanticMetricWaiverRequest,
    signal?: AbortSignal,
  ): Promise<Policy.RevalidatedSemanticWaiverResponse>;
  listSemanticPolicySkips(
    boardId: string,
    options?: Policy.SemanticSkipPageOptions,
  ): Promise<Policy.SemanticCursorPage<Policy.SemanticSkipListItem>>;
  createSemanticPolicySkip(
    boardId: string,
    request: Policy.CreateSemanticSkipRequest,
    idempotencyKey: string,
    signal?: AbortSignal,
  ): Promise<Policy.CreatedSemanticSkipResponse>;
  revokeSemanticPolicySkip(
    boardId: string,
    skipId: string,
    request: Policy.RevokeSemanticSkipRequest,
    signal?: AbortSignal,
  ): Promise<Policy.RevokedSemanticSkipResponse>;
}

export function createPolicyGovernanceApi(
  transport: PolicyGovernanceTransport,
): PolicyGovernanceApi {
  return {
    exportGuidelinePolicy(boardId, options = {}) {
      const params = new URLSearchParams();
      for (const guidelineId of options.guidelineIds ?? []) {
        params.append('guideline_ids', guidelineId);
      }
      params.set(
        'include_binding_history',
        String(options.includeBindingHistory ?? true),
      );
      return requestJson<Policy.GuidelineExportEnvelopeV3>(
        transport,
        `${boardRoot(boardId)}/guidelines/export?${params.toString()}`,
        { signal: options.signal },
      );
    },

    importGuidelinePolicy(boardId, envelope, options = {}) {
      const params = new URLSearchParams({
        dry_run: String(options.dryRun ?? false),
      });
      return postJson<Policy.GuidelineImportResult>(
        transport,
        `${boardRoot(boardId)}/guidelines/import?${params.toString()}`,
        envelope,
        options.signal,
      );
    },

    listGuidelineRevisions(boardId, guidelineId, options = {}) {
      return requestJson<
        Policy.PolicyCursorPage<Policy.GuidelineRevisionListItem>
      >(
        transport,
        `${guidelineRoot(boardId, guidelineId)}/revisions?${pageParams(
          options,
        ).toString()}`,
        { signal: options.signal },
      );
    },

    createGuidelineRevision(boardId, guidelineId, request, signal) {
      return postJson<Policy.CreateGuidelineRevisionResponse>(
        transport,
        `${guidelineRoot(boardId, guidelineId)}/revisions`,
        request,
        signal,
      );
    },

    getGuidelineRevision(
      boardId,
      guidelineId,
      revisionId,
      signal,
    ) {
      return requestJson<Policy.GuidelineRevisionAuthorityResponse>(
        transport,
        `${guidelineRoot(boardId, guidelineId)}/revisions/${encoded(
          revisionId,
        )}`,
        { signal },
      );
    },

    retireGuideline(boardId, guidelineId, request, signal) {
      return postJson<Policy.RetirementResponse>(
        transport,
        `${guidelineRoot(boardId, guidelineId)}/retire`,
        request,
        signal,
      );
    },

    previewGuidelineImpact(boardId, guidelineId, request, signal) {
      return postJson<Policy.GuidelineImpactPreviewResponse>(
        transport,
        `${guidelineRoot(boardId, guidelineId)}/impact-previews`,
        request,
        signal,
      );
    },

    getGuidelineImpact(
      boardId,
      guidelineId,
      previewId,
      signal,
    ) {
      return requestJson<Policy.GuidelineImpactPreviewResponse>(
        transport,
        `${guidelineRoot(boardId, guidelineId)}/impact-previews/${encoded(
          previewId,
        )}`,
        { signal },
      );
    },

    listGuidelineImpactItems(
      boardId,
      guidelineId,
      previewId,
      options = {},
    ) {
      const params = pageParams(options);
      setIfDefined(params, 'entity_type', options.entityType);
      setIfDefined(params, 'item_kind', options.itemKind);
      return requestJson<
        Policy.PolicyCursorPage<Policy.GuidelineImpactPageItem>
      >(
        transport,
        `${guidelineRoot(boardId, guidelineId)}/impact-previews/${encoded(
          previewId,
        )}/items?${params.toString()}`,
        { signal: options.signal },
      );
    },

    adoptGuidelineRevision(boardId, guidelineId, request, signal) {
      return postJson<Policy.GuidelineAdoptionResponse>(
        transport,
        `${guidelineRoot(boardId, guidelineId)}/adoptions`,
        request,
        signal,
      );
    },

    listSemanticGuidelineAssessments(boardId, options = {}) {
      const params = pageParams(options);
      setIfDefined(params, 'subject_type', options.subjectType);
      setIfDefined(params, 'subject_id', options.subjectId);
      setIfDefined(params, 'guideline_id', options.guidelineId);
      setIfDefined(params, 'binding_id', options.bindingId);
      setIfDefined(params, 'outcome', options.outcome);
      setIfDefined(params, 'currentness', options.currentness);
      setIfDefined(params, 'validation_edition', options.validationEdition);
      return requestJson<
        Policy.SemanticCursorPage<Policy.SemanticAssessmentListItem>
      >(
        transport,
        `${boardRoot(boardId)}/semantic-guideline-assessments?${params.toString()}`,
        { signal: options.signal },
      );
    },

    getCurrentSemanticGuidelineAssessment(
      boardId,
      entityType,
      subjectId,
      bindingId,
      projection = 'full',
      signal,
      validationEdition,
    ) {
      const params = new URLSearchParams({
        subject_type: entityType,
        subject_id: subjectId,
        binding_id: bindingId,
        projection,
      });
      if (validationEdition !== undefined) {
        params.set('validation_edition', String(validationEdition));
      }
      return requestJson<Policy.SemanticCurrentAssessmentResponse>(
        transport,
        `${boardRoot(boardId)}/semantic-guideline-assessments/current?${params.toString()}`,
        { signal },
      );
    },

    getSemanticGuidelineAssessment(
      boardId,
      receiptId,
      projection = 'full',
      signal,
    ) {
      const params = new URLSearchParams({ projection });
      return requestJson<Policy.SemanticAssessmentResponse>(
        transport,
        `${boardRoot(boardId)}/semantic-guideline-assessments/${encoded(
          receiptId,
        )}?${params.toString()}`,
        { signal },
      );
    },

    listSemanticGuidelineFindings(boardId, options = {}) {
      const params = pageParams(options);
      setIfDefined(params, 'receipt_id', options.receiptId);
      setIfDefined(params, 'guideline_id', options.guidelineId);
      setIfDefined(params, 'binding_id', options.bindingId);
      setIfDefined(params, 'metric_id', options.metricId);
      setIfDefined(params, 'subject_type', options.subjectType);
      setIfDefined(params, 'subject_id', options.subjectId);
      setIfDefined(params, 'outcome', options.outcome);
      return requestJson<
        Policy.SemanticCursorPage<Policy.SemanticFindingListItem>
      >(
        transport,
        `${boardRoot(boardId)}/semantic-guideline-findings?${params.toString()}`,
        { signal: options.signal },
      );
    },

    listSemanticMetricWaivers(boardId, options) {
      const params = pageParams(options);
      params.set('evaluated_at', options.evaluatedAt);
      setIfDefined(params, 'finding_id', options.findingId);
      setIfDefined(params, 'metric_result_id', options.metricResultId);
      setIfDefined(params, 'receipt_id', options.receiptId);
      setIfDefined(params, 'guideline_id', options.guidelineId);
      setIfDefined(params, 'binding_id', options.bindingId);
      setIfDefined(params, 'metric_id', options.metricId);
      setIfDefined(params, 'subject_type', options.subjectType);
      setIfDefined(params, 'subject_id', options.subjectId);
      setIfDefined(params, 'status', options.status);
      return requestJson<
        Policy.SemanticCursorPage<Policy.SemanticWaiverListItem>
      >(
        transport,
        `${boardRoot(boardId)}/policy-waivers?${params.toString()}`,
        { signal: options.signal },
      );
    },

    requestSemanticMetricWaiver(boardId, request, signal) {
      return postJson<Policy.RequestedSemanticWaiverResponse>(
        transport,
        `${boardRoot(boardId)}/policy-waivers`,
        request,
        signal,
      );
    },

    getSemanticMetricWaiver(
      boardId,
      waiverId,
      options,
    ) {
      const params = new URLSearchParams({
        evaluated_at: options.evaluatedAt,
        projection: options.projection ?? 'full',
      });
      return requestJson<Policy.SemanticWaiverResponse>(
        transport,
        `${waiverRoot(boardId, waiverId)}?${params.toString()}`,
        { signal: options.signal },
      );
    },

    listSemanticMetricWaiverEvents(boardId, waiverId, signal) {
      return requestJson<Policy.SemanticWaiverEventsResponse>(
        transport,
        `${waiverRoot(boardId, waiverId)}/events`,
        { signal },
      );
    },

    reviewSemanticMetricWaiver(
      boardId,
      waiverId,
      request,
      signal,
    ) {
      return postJson<Policy.ReviewedSemanticWaiverResponse>(
        transport,
        `${waiverRoot(boardId, waiverId)}/review`,
        request,
        signal,
      );
    },

    revokeSemanticMetricWaiver(
      boardId,
      waiverId,
      request,
      signal,
    ) {
      return postJson<Policy.RevokedSemanticWaiverResponse>(
        transport,
        `${waiverRoot(boardId, waiverId)}/revoke`,
        request,
        signal,
      );
    },

    revalidateSemanticMetricWaiver(
      boardId,
      waiverId,
      request,
      signal,
    ) {
      return postJson<Policy.RevalidatedSemanticWaiverResponse>(
        transport,
        `${waiverRoot(boardId, waiverId)}/revalidate`,
        request,
        signal,
      );
    },

    listSemanticPolicySkips(boardId, options = {}) {
      const params = pageParams(options);
      setIfDefined(params, 'subject_type', options.subjectType);
      setIfDefined(params, 'subject_id', options.subjectId);
      setIfDefined(params, 'binding_id', options.bindingId);
      setIfDefined(params, 'status', options.status);
      setIfDefined(params, 'currentness', options.currentness);
      return requestJson<
        Policy.SemanticCursorPage<Policy.SemanticSkipListItem>
      >(
        transport,
        `${boardRoot(boardId)}/semantic-guideline-skips?${params.toString()}`,
        { signal: options.signal },
      );
    },

    createSemanticPolicySkip(
      boardId,
      request,
      idempotencyKey,
      signal,
    ) {
      return requestJson<Policy.CreatedSemanticSkipResponse>(
        transport,
        `${boardRoot(boardId)}/semantic-guideline-skips`,
        {
          method: 'POST',
          headers: { 'Idempotency-Key': idempotencyKey },
          body: JSON.stringify(request),
          signal,
        },
      );
    },

    revokeSemanticPolicySkip(boardId, skipId, request, signal) {
      return postJson<Policy.RevokedSemanticSkipResponse>(
        transport,
        `${boardRoot(boardId)}/semantic-guideline-skips/${encoded(
          skipId,
        )}/revoke`,
        request,
        signal,
      );
    },
  };
}

export function usePolicyGovernanceApi(): PolicyGovernanceApi {
  const transport = useApiClient();
  return useMemo(() => createPolicyGovernanceApi(transport), [transport]);
}
