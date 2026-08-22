import { renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthenticatedFetchError } from '@/lib/authFetch';
import type {
  LegacyEvidenceClassificationBatchRequest,
  LegacyEvidenceClassificationBatchResult,
} from '@/types';
import {
  LegacyEvidenceClassificationConflictError,
  useDashboardApi,
} from '../api';

const mockApiClient = {
  fetchJson: vi.fn(),
  fetch: vi.fn(),
};

vi.mock('@/contexts/ApiContext', () => ({
  useApiClient: () => mockApiClient,
}));

function classificationRequest(): LegacyEvidenceClassificationBatchRequest {
  return {
    items: [
      {
        evidence_id: 'evidence-b',
        expected_evidence_payload_sha256: 'b'.repeat(64),
        expected_classification_revision: 1,
        source_role: 'existing_constraint',
        relevance_summary: 'The existing boundary constrains the change.',
        scope_relation: 'Same delivery scope.',
        source_origin: 'Committed repository baseline.',
        interpretation_limit: null,
        baseline_provenance: {
          presence: 'committed_snapshot',
          workspace_state_id: 'workspace-b',
          provenance_note: null,
        },
      },
      {
        evidence_id: 'evidence-a',
        expected_evidence_payload_sha256: 'a'.repeat(64),
        expected_classification_revision: 0,
        source_role: 'current_implementation',
        relevance_summary: 'The current behavior is directly relevant.',
        scope_relation: 'Same bounded feature.',
        source_origin: 'Observed implementation baseline.',
        interpretation_limit: null,
        baseline_provenance: {
          presence: 'preexisting_worktree',
          workspace_state_id: 'workspace-a',
          provenance_note: 'Scaffold present before delivery work began.',
        },
      },
    ],
    justification: 'Human review classified the ambiguous legacy Evidence.',
    idempotency_key: 'legacy-classification-1',
  };
}

function classificationResult(
  replayed = false,
): LegacyEvidenceClassificationBatchResult {
  const classifiedAt = '2026-08-22T12:00:00Z';
  const requestSha256 = 'c'.repeat(64);
  return {
    batch_id: 'batch-1',
    board_id: 'board-1',
    classified_by: 'user-1',
    classified_at: classifiedAt,
    request_sha256: requestSha256,
    classifications: [
      {
        id: 'classification-a',
        batch_id: 'batch-1',
        board_id: 'board-1',
        evidence_id: 'evidence-a',
        evidence_payload_sha256: 'a'.repeat(64),
        revision: 1,
        predecessor_classification_id: null,
        source_role: 'current_implementation',
        relevance_summary: 'The current behavior is directly relevant.',
        scope_relation: 'Same bounded feature.',
        source_origin: 'Observed implementation baseline.',
        interpretation_limit: null,
        baseline_provenance: {
          presence: 'preexisting_worktree',
          workspace_state_id: 'workspace-a',
          provenance_note: 'Scaffold present before delivery work began.',
        },
        classified_by: 'user-1',
        classified_at: classifiedAt,
        justification: 'Human review classified the ambiguous legacy Evidence.',
        request_sha256: requestSha256,
        batch_item_count: 2,
        batch_item_index: 1,
        context_contract_version: 2,
        classification_sha256: 'd'.repeat(64),
      },
      {
        id: 'classification-b',
        batch_id: 'batch-1',
        board_id: 'board-1',
        evidence_id: 'evidence-b',
        evidence_payload_sha256: 'b'.repeat(64),
        revision: 2,
        predecessor_classification_id: 'classification-b-previous',
        source_role: 'existing_constraint',
        relevance_summary: 'The existing boundary constrains the change.',
        scope_relation: 'Same delivery scope.',
        source_origin: 'Committed repository baseline.',
        interpretation_limit: null,
        baseline_provenance: {
          presence: 'committed_snapshot',
          workspace_state_id: 'workspace-b',
          provenance_note: null,
        },
        classified_by: 'user-1',
        classified_at: classifiedAt,
        justification: 'Human review classified the ambiguous legacy Evidence.',
        request_sha256: requestSha256,
        batch_item_count: 2,
        batch_item_index: 2,
        context_contract_version: 2,
        classification_sha256: 'e'.repeat(64),
      },
    ],
    replayed,
  };
}

beforeEach(() => {
  mockApiClient.fetchJson.mockReset();
  mockApiClient.fetch.mockReset();
});

describe('legacy Evidence classification REST transaction', () => {
  it('ts_15a6a9dc — posts one closed atomic batch and leaves one canonical refetch to the consumer', async () => {
    const request = Object.assign(classificationRequest(), {
      board_id: 'must-not-cross-the-wire',
      expected_subject_version: 41,
    });
    Object.assign(request.items[0], {
      declared_source_content_sha256: 'f'.repeat(64),
      expected_subject_version: 9,
    });
    const receipt = classificationResult();
    const controller = new AbortController();
    mockApiClient.fetchJson.mockResolvedValueOnce(receipt);
    const { result } = renderHook(() => useDashboardApi());

    const committed = await result.current.classifyLegacyCodeEvidence(
      'board/1',
      request,
      controller.signal,
    );

    expect(committed).toBe(receipt);
    expect(mockApiClient.fetchJson).toHaveBeenCalledTimes(1);
    const [path, options] = mockApiClient.fetchJson.mock.calls[0];
    expect(path).toBe('/boards/board%2F1/code-evidence/legacy-classifications');
    expect(options).toMatchObject({
      method: 'POST',
      signal: controller.signal,
    });
    const body = JSON.parse(options.body as string) as Record<string, unknown>;
    expect(Object.keys(body)).toEqual(['items', 'justification', 'idempotency_key']);
    const items = body.items as Array<Record<string, unknown>>;
    expect(items.map((item) => item.evidence_id)).toEqual([
      'evidence-a',
      'evidence-b',
    ]);
    expect(Object.keys(items[0])).toEqual([
      'evidence_id',
      'expected_evidence_payload_sha256',
      'expected_classification_revision',
      'source_role',
      'relevance_summary',
      'scope_relation',
      'source_origin',
      'interpretation_limit',
      'baseline_provenance',
    ]);
    expect(Object.keys(items[0].baseline_provenance as object)).toEqual([
      'presence',
      'workspace_state_id',
      'provenance_note',
    ]);
    expect(options.body).not.toContain('expected_subject_version');
    expect(options.body).not.toContain('declared_source_content_sha256');

    mockApiClient.fetchJson.mockResolvedValueOnce({});
    await result.current.getCodeTraceabilityProjection(
      'board/1',
      'spec',
      'spec/1',
      7,
      { profile: 'full' },
    );
    expect(mockApiClient.fetchJson).toHaveBeenCalledTimes(2);
    expect(mockApiClient.fetchJson).toHaveBeenNthCalledWith(
      2,
      '/boards/board%2F1/code-traceability-projection?subject_type=spec&subject_id=spec%2F1&subject_version=7&profile=full&context_scope=default',
      { signal: undefined },
    );
  });

  it('ts_8b303869 — serializes an exact retry identically and preserves the replay receipt', async () => {
    const request = classificationRequest();
    const originalRequest = JSON.stringify(request);
    const initial = classificationResult(false);
    const replay = classificationResult(true);
    mockApiClient.fetchJson
      .mockResolvedValueOnce(initial)
      .mockResolvedValueOnce(replay);
    const { result } = renderHook(() => useDashboardApi());

    await expect(
      result.current.classifyLegacyCodeEvidence('board-1', request),
    ).resolves.toBe(initial);
    await expect(
      result.current.classifyLegacyCodeEvidence('board-1', request),
    ).resolves.toBe(replay);

    expect(mockApiClient.fetchJson).toHaveBeenCalledTimes(2);
    expect(mockApiClient.fetchJson.mock.calls[0][1].body).toBe(
      mockApiClient.fetchJson.mock.calls[1][1].body,
    );
    expect(JSON.stringify(request)).toBe(originalRequest);
    expect(replay.replayed).toBe(true);
  });

  it.each([
    ['payload', 'code_evidence_legacy_classification_payload_conflict'],
    ['revision', 'code_evidence_legacy_classification_revision_conflict'],
    ['idempotency', 'code_evidence_legacy_classification_idempotency_conflict'],
  ] as const)('ts_8b303869 — preserves the typed %s conflict', async (kind, code) => {
    const transportError = new AuthenticatedFetchError({
      message: code,
      status: 409,
      code,
      details: {
        evidence_id: 'evidence-a',
        expected_revision: 0,
        current_revision: 1,
      },
    });
    mockApiClient.fetchJson.mockRejectedValueOnce(transportError);
    const { result } = renderHook(() => useDashboardApi());

    let caught: unknown;
    try {
      await result.current.classifyLegacyCodeEvidence(
        'board-1',
        classificationRequest(),
      );
    } catch (error) {
      caught = error;
    }

    expect(caught).toBeInstanceOf(LegacyEvidenceClassificationConflictError);
    expect(caught).toMatchObject({
      name: 'LegacyEvidenceClassificationConflictError',
      status: 409,
      code,
      kind,
      retryable: false,
      details: {
        evidence_id: 'evidence-a',
        expected_revision: 0,
        current_revision: 1,
      },
    });
    expect(
      (caught as LegacyEvidenceClassificationConflictError).transportError,
    ).toBe(transportError);
  });

  it('ts_8b303869 — does not relabel unrelated transport failures as classification conflicts', async () => {
    const unavailable = new AuthenticatedFetchError({
      message: 'Service unavailable',
      status: 503,
      code: 'code_evidence_legacy_classification_persistence_conflict',
      retryable: true,
    });
    mockApiClient.fetchJson.mockRejectedValueOnce(unavailable);
    const { result } = renderHook(() => useDashboardApi());

    await expect(
      result.current.classifyLegacyCodeEvidence(
        'board-1',
        classificationRequest(),
      ),
    ).rejects.toBe(unavailable);
  });

  it.each([
    [403, 'code_evidence_classification_forbidden'],
    [404, 'code_evidence_not_found'],
    [422, 'code_evidence_legacy_classification_validation_failed'],
  ] as const)('ts_8b303869 — preserves HTTP %s for the drawer recovery policy', async (status, code) => {
    const transportError = new AuthenticatedFetchError({
      message: code,
      status,
      code,
      details: status === 422
        ? { errors: [{ loc: ['body', 'items', 0, 'source_role'], msg: 'Invalid role.' }] }
        : null,
    });
    mockApiClient.fetchJson.mockRejectedValueOnce(transportError);
    const { result } = renderHook(() => useDashboardApi());

    await expect(
      result.current.classifyLegacyCodeEvidence(
        'board-1',
        classificationRequest(),
      ),
    ).rejects.toBe(transportError);
    expect(mockApiClient.fetchJson).toHaveBeenCalledTimes(1);
  });
});
