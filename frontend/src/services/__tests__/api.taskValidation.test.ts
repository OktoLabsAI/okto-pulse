import { renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { TaskValidationSubmitPayload, ValidationEntry } from '@/types';
import { useDashboardApi } from '../api';

const mockApiClient = vi.hoisted(() => ({
  fetchJson: vi.fn(),
  fetch: vi.fn(),
}));

vi.mock('@/contexts/ApiContext', () => ({
  useApiClient: () => mockApiClient,
}));

describe('task validation REST client', () => {
  beforeEach(() => {
    mockApiClient.fetchJson.mockReset();
    mockApiClient.fetch.mockReset();
  });

  it('sends the backend write contract and returns the validation entry', async () => {
    const payload: TaskValidationSubmitPayload = {
      confidence: 91,
      confidence_justification: 'The evidence is complete and reproducible.',
      estimated_completeness: 94,
      completeness_justification: 'All acceptance paths were implemented.',
      estimated_drift: 8,
      drift_justification: 'Only a documented naming adjustment was made.',
      general_justification: 'The implementation satisfies the task and its linked requirements.',
      recommendation: 'approve',
    };
    const response: ValidationEntry = {
      id: 'val-1',
      card_id: 'card-1',
      board_id: 'board-1',
      reviewer_id: 'reviewer-1',
      evaluator_id: 'reviewer-1',
      confidence: 91,
      confidence_justification: payload.confidence_justification,
      estimated_completeness: 94,
      completeness: 94,
      completeness_justification: payload.completeness_justification,
      estimated_drift: 8,
      drift: 8,
      drift_justification: payload.drift_justification,
      general_justification: payload.general_justification,
      summary: payload.general_justification,
      recommendation: 'approve',
      outcome: 'success',
      verdict: 'pass',
      threshold_violations: [],
      resolved_thresholds: {
        required: true,
        min_confidence: 70,
        min_completeness: 80,
        max_drift: 50,
        resolved_from: 'spec',
        resolved_sources: {
          required: 'spec',
          min_confidence: 'board',
          min_completeness: 'board',
          max_drift: 'sprint',
        },
      },
      reviewer_separation: {
        mode: 'enforce',
        allowed: true,
        warning: false,
        conflicts: [],
        source: 'board_settings',
      },
      card_status: 'done',
      created_at: '2026-07-29T12:00:00Z',
    };
    mockApiClient.fetchJson.mockResolvedValue(response);
    const { result } = renderHook(() => useDashboardApi());

    const validation: ValidationEntry = await result.current.submitTaskValidation(
      'card-1',
      payload,
    );

    expect(mockApiClient.fetchJson).toHaveBeenCalledWith(
      '/cards/card-1/validate',
      {
        method: 'POST',
        body: JSON.stringify(payload),
      },
    );
    expect(validation).toEqual(response);
  });

  it('keeps legacy-only validation aliases representable', () => {
    const legacyEntry: ValidationEntry = {
      id: 'legacy-val-1',
      reviewer_id: 'legacy-reviewer',
      confidence: 75,
      estimated_completeness: 82,
      estimated_drift: 20,
      general_justification: 'Historical entry created before clean aliases were dual-written.',
      recommendation: 'approve',
      outcome: 'success',
      threshold_violations: [],
      created_at: '2025-12-01T10:00:00Z',
    };

    expect(legacyEntry.evaluator_id ?? legacyEntry.reviewer_id).toBe(
      'legacy-reviewer',
    );
    expect(
      legacyEntry.completeness ?? legacyEntry.estimated_completeness,
    ).toBe(82);
    expect(legacyEntry.drift ?? legacyEntry.estimated_drift).toBe(20);
    expect(legacyEntry.summary ?? legacyEntry.general_justification).toContain(
      'Historical entry',
    );
  });
});
