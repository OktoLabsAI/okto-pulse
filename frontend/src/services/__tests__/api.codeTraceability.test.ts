import { renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type {
  CodeEvidenceRevokeRequest,
  CodeTraceabilityWaiverCreateRequest,
  ImplementationTargetCreateRequest,
  TargetOverlapAcknowledgementRequest,
} from '@/types';
import { useDashboardApi } from '../api';

const mockApiClient = {
  fetchJson: vi.fn(),
  fetch: vi.fn(),
};

vi.mock('@/contexts/ApiContext', () => ({
  useApiClient: () => mockApiClient,
}));

beforeEach(() => {
  mockApiClient.fetchJson.mockReset();
  mockApiClient.fetch.mockReset();
  mockApiClient.fetchJson.mockResolvedValue({});
});

describe('Code Traceability REST client', () => {
  it('sends a target intent to the board/card-scoped endpoint without server-owned fields', async () => {
    const payload: ImplementationTargetCreateRequest = {
      source_ref: 'source:opaque-1',
      selector_kind: 'symbol',
      relative_path_hint: null,
      language: null,
      symbol_kind: null,
      qualified_symbol: 'PaymentsService.authorize',
      symbol_signature: null,
      role: 'modify',
      intent: 'Authorize before persistence.',
      required: true,
      expected_spec_version: 7,
      baseline_evidence_id: null,
      spec_links: [],
      evidence_links: [],
    };
    const { result } = renderHook(() => useDashboardApi());

    await result.current.createImplementationTarget('board/1', 'card/1', payload);

    expect(mockApiClient.fetchJson).toHaveBeenCalledWith(
      '/boards/board%2F1/cards/card%2F1/implementation-targets',
      { method: 'POST', body: JSON.stringify(payload) },
    );
    expect(payload).not.toHaveProperty('board_id');
    expect(payload).not.toHaveProperty('card_id');
    expect(payload).not.toHaveProperty('investigation_receipt_id');
  });

  it('sends exactly the immutable overlap pair, disposition and justification', async () => {
    const payload: TargetOverlapAcknowledgementRequest = {
      target_a_id: 'target-1',
      target_b_id: 'target-2',
      resolution_a_id: 'resolution-1',
      resolution_b_id: 'resolution-2',
      disposition: 'ordered_by_dependency',
      justification: 'Task 2 runs after Task 1.',
    };
    const { result } = renderHook(() => useDashboardApi());

    await result.current.acknowledgeImplementationOverlap('board-1', 'card-1', payload);

    expect(mockApiClient.fetchJson).toHaveBeenCalledWith(
      '/boards/board-1/cards/card-1/implementation-overlaps/acknowledgements',
      { method: 'POST', body: JSON.stringify(payload) },
    );
    expect(Object.keys(payload).sort()).toEqual([
      'disposition',
      'justification',
      'resolution_a_id',
      'resolution_b_id',
      'target_a_id',
      'target_b_id',
    ]);
  });

  it('revokes Evidence with only the human reason field', async () => {
    const payload: CodeEvidenceRevokeRequest = {
      reason: 'This Evidence was attached to the wrong refinement revision.',
    };
    const { result } = renderHook(() => useDashboardApi());

    await result.current.revokeCodeEvidence('board/1', 'evidence/1', payload);

    expect(mockApiClient.fetchJson).toHaveBeenCalledWith(
      '/boards/board%2F1/code-evidence/evidence%2F1/revoke',
      { method: 'POST', body: JSON.stringify(payload) },
    );
    expect(Object.keys(payload)).toEqual(['reason']);
  });

  it('creates and clears a dedicated human waiver without attestation fields', async () => {
    const payload: CodeTraceabilityWaiverCreateRequest = {
      entity_type: 'card',
      entity_id: 'card-1',
      scope: 'target_resolution',
      reason_code: 'external_source_unavailable',
      justification: 'The source is unavailable for this release window.',
    };
    const { result } = renderHook(() => useDashboardApi());

    await result.current.createCodeTraceabilityWaiver('board/1', payload);
    await result.current.clearCodeTraceabilityWaiver('board/1', 'waiver/1');

    expect(mockApiClient.fetchJson).toHaveBeenNthCalledWith(
      1,
      '/boards/board%2F1/code-traceability-waivers',
      { method: 'POST', body: JSON.stringify(payload) },
    );
    expect(mockApiClient.fetchJson).toHaveBeenNthCalledWith(
      2,
      '/boards/board%2F1/code-traceability-waivers/waiver%2F1',
      { method: 'DELETE' },
    );
    expect(payload).not.toHaveProperty('board_id');
    expect(payload).not.toHaveProperty('investigation_receipt_id');
    expect(Object.keys(payload).sort()).toEqual([
      'entity_id',
      'entity_type',
      'justification',
      'reason_code',
      'scope',
    ]);
  });
});
