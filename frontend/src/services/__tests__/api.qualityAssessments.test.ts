import { renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthenticatedFetchError } from '@/lib/authFetch';
import { useDashboardApi } from '../api';

const mockApiClient = {
  fetchJson: vi.fn(),
  fetch: vi.fn(),
};

vi.mock('@/contexts/ApiContext', () => ({
  useApiClient: () => mockApiClient,
}));

describe('quality assessment REST client', () => {
  beforeEach(() => {
    mockApiClient.fetchJson.mockReset();
    mockApiClient.fetch.mockReset();
    mockApiClient.fetchJson.mockResolvedValue({
      items: [],
      total_filtered: 0,
      total_overall: 0,
      offset: 0,
      limit: 25,
    });
  });

  it('uses the closed subject paths, filters and supported 25/50/100 windows', async () => {
    const controller = new AbortController();
    const { result } = renderHook(() => useDashboardApi());

    await result.current.listQualityAssessments('ideation', 'idea/1', {
      offset: 25,
      limit: 25,
      assessmentKind: 'ambiguity',
      state: 'stale',
      signal: controller.signal,
    });
    await result.current.listQualityFindings('refinement', 'ref-1', {
      offset: 50,
      limit: 50,
      receiptId: 'receipt-1',
      assessmentKind: 'ambiguity',
      categoryCode: ' acceptance_measurability ',
      severity: 'high',
      signal: controller.signal,
    });
    await result.current.listQualityAssessmentReceiptFindings('receipt/1', {
      offset: 100,
      limit: 100,
      categoryCode: 'domain_data_model',
      severity: 'low',
      signal: controller.signal,
    });

    expect(mockApiClient.fetchJson).toHaveBeenNthCalledWith(
      1,
      '/ideations/idea%2F1/quality-assessments?offset=25&limit=25&assessment_kind=ambiguity&state=stale',
      { signal: controller.signal },
    );
    expect(mockApiClient.fetchJson).toHaveBeenNthCalledWith(
      2,
      '/refinements/ref-1/quality-findings?offset=50&limit=50&receipt_id=receipt-1&assessment_kind=ambiguity&category_code=acceptance_measurability&severity=high',
      { signal: controller.signal },
    );
    expect(mockApiClient.fetchJson).toHaveBeenNthCalledWith(
      3,
      '/quality-assessment-receipts/receipt%2F1/findings?offset=100&limit=100&category_code=domain_data_model&severity=low',
      { signal: controller.signal },
    );
  });

  it('returns null only for a missing current receipt and propagates other failures', async () => {
    const { result } = renderHook(() => useDashboardApi());
    mockApiClient.fetchJson.mockRejectedValueOnce(
      new AuthenticatedFetchError({
        message: 'No head',
        status: 404,
        code: 'not_found',
        details: { reason_code: 'assessment_current_not_found' },
      }),
    );

    await expect(
      result.current.getCurrentQualityAssessment(
        'spec',
        'spec-1',
        'spec_validation',
      ),
    ).resolves.toBeNull();

    const missingSubject = new AuthenticatedFetchError({
      message: 'Subject missing',
      status: 404,
      code: 'not_found',
      details: { reason_code: 'assessment_subject_not_found' },
    });
    mockApiClient.fetchJson.mockRejectedValueOnce(missingSubject);
    await expect(
      result.current.getCurrentQualityAssessment(
        'spec',
        'missing-spec',
        'spec_validation',
      ),
    ).rejects.toBe(missingSubject);

    const forbidden = new AuthenticatedFetchError({
      message: 'Forbidden',
      status: 403,
    });
    mockApiClient.fetchJson.mockRejectedValueOnce(forbidden);
    await expect(
      result.current.getCurrentQualityAssessment(
        'spec',
        'spec-1',
        'requirement_lint',
      ),
    ).rejects.toBe(forbidden);
  });

  it('reads receipt detail and sends the governed manual payload unchanged', async () => {
    const { result } = renderHook(() => useDashboardApi());
    const payload = {
      idempotency_key: 'idem-1',
      expected_subject_version: 7,
      expected_head_revision: 2,
      score: 3,
      findings: [],
      proposed_questions: [],
    };

    await result.current.getQualityAssessmentReceipt('receipt-1');
    await result.current.recordAmbiguityAssessment(
      'refinement',
      'refinement-1',
      payload,
    );

    expect(mockApiClient.fetchJson).toHaveBeenNthCalledWith(
      1,
      '/quality-assessment-receipts/receipt-1',
      { signal: undefined },
    );
    expect(mockApiClient.fetchJson).toHaveBeenNthCalledWith(
      2,
      '/refinements/refinement-1/quality-assessments',
      {
        method: 'POST',
        body: JSON.stringify(payload),
      },
    );
  });
});
