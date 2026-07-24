import { renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  Card,
  DeriveSpecKnowledgeRequest,
  KnowledgeAssignmentDropRequest,
  KnowledgeAssignmentRefreshRequest,
  KnowledgeAssignmentReplaceRequest,
} from '@/types';
import { useDashboardApi } from '../api';

const mockApiClient = {
  fetchJson: vi.fn(),
  fetch: vi.fn(),
};

vi.mock('@/contexts/ApiContext', () => ({
  useApiClient: () => mockApiClient,
}));

describe('selective Knowledge propagation API surface', () => {
  beforeEach(() => {
    mockApiClient.fetchJson.mockReset();
    mockApiClient.fetch.mockReset();
  });

  it('normalizes the additive v2 create receipt back to Card', async () => {
    const card = { id: 'card-1', title: 'Governed card' } as Card;
    mockApiClient.fetchJson.mockResolvedValue({
      contract_version: 2,
      card,
      operation_id: 'op-1',
      revision: 1,
      replayed: false,
      selection_state: 'explicit_ids',
      assignments: [],
    });
    const { result } = renderHook(() => useDashboardApi());

    const created = await result.current.createCard('board-1', {
      title: 'Governed card',
      knowledge_propagation: {
        selection_state: 'explicit_ids',
        mode: 'reference',
        knowledge_ids: ['kb-1'],
        justification: 'Required by AC-1',
        idempotency_key: 'create-card-1',
        relevance_links: [
          { entity_type: 'acceptance_criterion', entity_id: 'ac-1' },
        ],
      },
    });

    expect(created).toBe(card);
    expect(mockApiClient.fetchJson).toHaveBeenCalledWith(
      '/boards/board-1/cards',
      {
        method: 'POST',
        body: JSON.stringify({
          title: 'Governed card',
          knowledge_propagation: {
            selection_state: 'explicit_ids',
            mode: 'reference',
            knowledge_ids: ['kb-1'],
            justification: 'Required by AC-1',
            idempotency_key: 'create-card-1',
            relevance_links: [
              { entity_type: 'acceptance_criterion', entity_id: 'ac-1' },
            ],
          },
        }),
      },
    );
  });

  it('keeps the legacy create and body-less derive paths unchanged', async () => {
    const card = { id: 'card-legacy' } as Card;
    const spec = { id: 'spec-legacy' };
    mockApiClient.fetchJson
      .mockResolvedValueOnce(card)
      .mockResolvedValueOnce(spec);
    const { result } = renderHook(() => useDashboardApi());

    expect(
      await result.current.createCard('board-1', { title: 'Legacy card' }),
    ).toBe(card);
    expect(
      await result.current.deriveSpecFromRefinement('refinement-1'),
    ).toBe(spec);
    expect(mockApiClient.fetchJson).toHaveBeenNthCalledWith(
      2,
      '/refinements/refinement-1/derive-spec',
      { method: 'POST' },
    );
  });

  it('sends an optional v2 envelope when deriving a spec', async () => {
    const data: DeriveSpecKnowledgeRequest = {
      knowledge_propagation: {
        selection_state: 'explicit_empty',
        mode: 'drop',
        knowledge_ids: [],
        justification: 'No KB applies to this spec',
        idempotency_key: 'derive-spec-1',
      },
    };
    mockApiClient.fetchJson.mockResolvedValue({
      contract_version: 2,
      target_type: 'spec',
      target_id: 'spec-1',
      spec_id: 'spec-1',
      operation_id: 'op-derive',
      revision: 1,
      replayed: false,
      selection_state: 'explicit_empty',
      assignments: [],
    });
    const { result } = renderHook(() => useDashboardApi());

    const response = await result.current.deriveSpecFromRefinement(
      'refinement-1',
      data,
    );

    expect(response.spec_id).toBe('spec-1');
    expect(mockApiClient.fetchJson).toHaveBeenCalledWith(
      '/refinements/refinement-1/derive-spec',
      {
        method: 'POST',
        body: JSON.stringify(data),
      },
    );
  });

  it('routes assignment read, replace, drop and refresh through v2 endpoints', async () => {
    mockApiClient.fetchJson.mockResolvedValue({});
    const replace: KnowledgeAssignmentReplaceRequest = {
      knowledge_ids: ['kb-1'],
      mode: 'snapshot',
      justification: 'Freeze evidence for test execution',
      idempotency_key: 'replace-1',
      expected_revision: 2,
      linkage: [{ entity_type: 'test_scenario', entity_id: 'ts-1' }],
    };
    const drop: KnowledgeAssignmentDropRequest = {
      knowledge_ids: ['kb-1'],
      justification: 'Knowledge no longer applies',
      idempotency_key: 'drop-1',
      expected_revision: 3,
    };
    const refresh: KnowledgeAssignmentRefreshRequest = {
      knowledge_ids: ['kb-2'],
      idempotency_key: 'refresh-1',
      expected_revision: 4,
    };
    const { result } = renderHook(() => useDashboardApi());

    await result.current.getCardKnowledgeAssignments('card-1');
    await result.current.replaceCardKnowledgeAssignments('card-1', replace);
    await result.current.dropCardKnowledgeAssignments('card-1', drop);
    await result.current.refreshCardKnowledgeAssignments('card-1', refresh);

    expect(mockApiClient.fetchJson).toHaveBeenNthCalledWith(
      1,
      '/cards/card-1/knowledge-assignments',
    );
    expect(mockApiClient.fetchJson).toHaveBeenNthCalledWith(
      2,
      '/cards/card-1/knowledge-assignments',
      { method: 'PUT', body: JSON.stringify(replace) },
    );
    expect(mockApiClient.fetchJson).toHaveBeenNthCalledWith(
      3,
      '/cards/card-1/knowledge-assignments/drop',
      { method: 'POST', body: JSON.stringify(drop) },
    );
    expect(mockApiClient.fetchJson).toHaveBeenNthCalledWith(
      4,
      '/cards/card-1/knowledge-assignments/refresh',
      { method: 'POST', body: JSON.stringify(refresh) },
    );
  });
});
