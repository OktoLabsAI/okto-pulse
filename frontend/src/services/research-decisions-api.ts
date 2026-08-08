/**
 * Authenticated, standalone REST client for the Refinement Research Decision
 * Ledger. It deliberately does not extend the monolithic dashboard API.
 */

import { useMemo } from 'react';

import { useApiClient } from '@/contexts/ApiContext';
import type {
  AppendResearchDecisionRequest,
  ListResearchDecisionsOptions,
  ResearchDecisionErrorDetails,
  ResearchDecisionHeadResponse,
  ResearchDecisionHeadResolution,
  ResearchDecisionPage,
  ResearchDecisionWriteResponse,
  SupersedeResearchDecisionRequest,
} from '@/types/research-decisions';

const REFINEMENTS_ROOT = '/refinements';

export interface ResearchDecisionTransport {
  fetch(url: string, options?: RequestInit): Promise<Response>;
}

export class ResearchDecisionApiError extends Error {
  readonly status: number;
  readonly code: string | null;
  readonly retryable: boolean;
  readonly details: ResearchDecisionErrorDetails | null;

  constructor({
    message,
    status,
    code = null,
    retryable = false,
    details = null,
  }: {
    message: string;
    status: number;
    code?: string | null;
    retryable?: boolean;
    details?: ResearchDecisionErrorDetails | null;
  }) {
    super(message);
    this.name = 'ResearchDecisionApiError';
    this.status = status;
    this.code = code;
    this.retryable = retryable;
    this.details = details;
  }
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null
    ? (value as Record<string, unknown>)
    : null;
}

async function toApiError(response: Response): Promise<ResearchDecisionApiError> {
  const body = asRecord(await response.json().catch(() => null));
  const detail = asRecord(body?.detail) ?? body;
  const codeValue =
    detail?.error_code ??
    detail?.code ??
    detail?.error ??
    body?.error_code ??
    body?.code ??
    body?.error;
  const messageValue = detail?.message ?? body?.message;
  const details = asRecord(detail?.details ?? body?.details);

  return new ResearchDecisionApiError({
    status: response.status,
    code: typeof codeValue === 'string' ? codeValue : null,
    message:
      typeof messageValue === 'string'
        ? messageValue
        : typeof codeValue === 'string'
          ? codeValue
          : `HTTP ${response.status}: ${response.statusText}`,
    retryable: Boolean(detail?.retryable ?? body?.retryable),
    details,
  });
}

async function requestJson<T>(
  transport: ResearchDecisionTransport,
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const headers = new Headers(options.headers);
  if (!headers.has('Accept')) headers.set('Accept', 'application/json');
  if (options.body !== undefined && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }

  const response = await transport.fetch(path, { ...options, headers });
  if (!response.ok) throw await toApiError(response);
  return (await response.json()) as T;
}

function researchDecisionPath(refinementId: string): string {
  return `${REFINEMENTS_ROOT}/${encodeURIComponent(
    refinementId,
  )}/research-decisions`;
}

export interface ResearchDecisionsApi {
  listResearchDecisions(
    refinementId: string,
    options?: ListResearchDecisionsOptions,
  ): Promise<ResearchDecisionPage>;
  appendResearchDecision(
    refinementId: string,
    request: AppendResearchDecisionRequest,
    signal?: AbortSignal,
  ): Promise<ResearchDecisionWriteResponse>;
  supersedeResearchDecision(
    refinementId: string,
    request: SupersedeResearchDecisionRequest,
    signal?: AbortSignal,
  ): Promise<ResearchDecisionWriteResponse>;
  resolveResearchDecisionHead(
    refinementId: string,
    ledgerId: string,
    signal?: AbortSignal,
  ): Promise<ResearchDecisionHeadResolution>;
}

export function createResearchDecisionsApi(
  transport: ResearchDecisionTransport,
): ResearchDecisionsApi {
  const listResearchDecisions = (
    refinementId: string,
    options: ListResearchDecisionsOptions = {},
  ): Promise<ResearchDecisionPage> => {
    const params = new URLSearchParams({
      offset: String(options.offset ?? 0),
      limit: String(options.limit ?? 25),
    });
    const ledgerId = options.ledgerId?.trim();
    if (ledgerId) params.set('ledger_id', ledgerId);
    if (options.status) params.set('status', options.status);

    return requestJson<ResearchDecisionPage>(
      transport,
      `${researchDecisionPath(refinementId)}?${params.toString()}`,
      { signal: options.signal },
    );
  };

  const write = (
    refinementId: string,
    request: AppendResearchDecisionRequest | SupersedeResearchDecisionRequest,
    signal?: AbortSignal,
  ): Promise<ResearchDecisionWriteResponse> =>
    requestJson<ResearchDecisionWriteResponse>(
      transport,
      researchDecisionPath(refinementId),
      {
        method: 'POST',
        body: JSON.stringify(request),
        signal,
      },
    );

  return {
    listResearchDecisions,
    appendResearchDecision: (refinementId, request, signal) =>
      write(refinementId, request, signal),
    supersedeResearchDecision: (refinementId, request, signal) =>
      write(refinementId, request, signal),
    async resolveResearchDecisionHead(refinementId, ledgerId, signal) {
      const response = await requestJson<ResearchDecisionHeadResponse>(
        transport,
        `${researchDecisionPath(refinementId)}/head?${new URLSearchParams({
          ledger_id: ledgerId,
        }).toString()}`,
        { signal },
      );
      return {
        entry: response.entry,
        headRevision: response.head_revision,
      };
    },
  };
}

export function useResearchDecisionsApi(): ResearchDecisionsApi {
  const transport = useApiClient();
  return useMemo(() => createResearchDecisionsApi(transport), [transport]);
}
