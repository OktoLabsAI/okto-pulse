import { renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { specEntityTypeForField, useDashboardApi } from '../api';

const mockApiClient = {
  fetchJson: vi.fn(),
  fetch: vi.fn(),
};

vi.mock('@/contexts/ApiContext', () => ({
  useApiClient: () => mockApiClient,
}));

function okResult(operation: string) {
  return {
    success: true,
    entity_type: 'business_rule',
    operation,
    spec_id: 'spec-1',
    entity_id: 'br-1',
    child_ref: 'spec:spec-1:business_rule:br-1',
    spec_version: 8,
    changed_fields: ['business_rules'],
    error_code: null,
    error_message: null,
    required_permission: null,
    impact_report: null,
    ack_token: null,
    expires_at: null,
  };
}

describe('structured spec entity API surface', () => {
  beforeEach(() => {
    mockApiClient.fetchJson.mockReset();
    mockApiClient.fetch.mockReset();
  });

  it('routes CRUD and impact preview through structured-entities endpoints', async () => {
    mockApiClient.fetchJson.mockResolvedValue(okResult('create'));
    const { result } = renderHook(() => useDashboardApi());

    await result.current.createSpecEntity('spec-1', 'business_rule', { id: 'br-1', title: 'Rule' }, 7);
    await result.current.updateSpecEntity('spec-1', 'business_rule', 'br-1', { title: 'Rule v2' }, 8);
    await result.current.operateSpecEntity('spec-1', 'business_rule', 'br-1', 'link_task', { task_id: 'card-1' });
    await result.current.previewSpecEntityImpact('spec-1', 'business_rule', 'br-1', 'revoke', {
      expected_spec_version: 9,
    });

    expect(mockApiClient.fetchJson).toHaveBeenNthCalledWith(
      1,
      '/specs/spec-1/structured-entities/business_rule',
      {
        method: 'POST',
        body: JSON.stringify({
          payload: { id: 'br-1', title: 'Rule' },
          expected_spec_version: 7,
        }),
      },
    );
    expect(mockApiClient.fetchJson).toHaveBeenNthCalledWith(
      2,
      '/specs/spec-1/structured-entities/business_rule/br-1',
      {
        method: 'PATCH',
        body: JSON.stringify({
          operation: 'update',
          payload: { title: 'Rule v2' },
          expected_spec_version: 8,
        }),
      },
    );
    expect(mockApiClient.fetchJson).toHaveBeenNthCalledWith(
      3,
      '/specs/spec-1/structured-entities/business_rule/br-1',
      {
        method: 'POST',
        body: JSON.stringify({
          task_id: 'card-1',
          operation: 'link_task',
        }),
      },
    );
    expect(mockApiClient.fetchJson).toHaveBeenNthCalledWith(
      4,
      '/specs/spec-1/structured-entities/business_rule/br-1/impact-preview',
      {
        method: 'POST',
        body: JSON.stringify({
          expected_spec_version: 9,
          operation: 'revoke',
        }),
      },
    );
  });

  it('links tasks via structured operation and reloads the spec without whole-list PATCH', async () => {
    mockApiClient.fetchJson.mockImplementation(async (url: string, options?: RequestInit) => {
      if (url.includes('/structured-entities/')) return okResult('link_task');
      if (url === '/specs/spec-1' && !options) return { id: 'spec-1', business_rules: [] };
      throw new Error(`Unexpected call: ${url}`);
    });
    const { result } = renderHook(() => useDashboardApi());

    await result.current.linkTaskToSpecItem('spec-1', 'business_rules', 'br-1', 'card-1');

    expect(mockApiClient.fetchJson).toHaveBeenCalledWith(
      '/specs/spec-1/structured-entities/business_rule/br-1',
      {
        method: 'POST',
        body: JSON.stringify({
          task_id: 'card-1',
          operation: 'link_task',
        }),
      },
    );
    expect(mockApiClient.fetchJson).not.toHaveBeenCalledWith(
      '/specs/spec-1',
      expect.objectContaining({ method: 'PATCH' }),
    );
  });

  it('unlinks tasks via structured operation and reloads the spec without whole-list PATCH', async () => {
    mockApiClient.fetchJson.mockImplementation(async (url: string, options?: RequestInit) => {
      if (url.includes('/structured-entities/')) return okResult('unlink_task');
      if (url === '/specs/spec-1' && !options) return { id: 'spec-1', business_rules: [] };
      throw new Error(`Unexpected call: ${url}`);
    });
    const { result } = renderHook(() => useDashboardApi());

    await result.current.unlinkTaskFromSpecItem('spec-1', 'decisions', 'dec-1', 'card-1');

    expect(mockApiClient.fetchJson).toHaveBeenCalledWith(
      '/specs/spec-1/structured-entities/decision/dec-1',
      {
        method: 'POST',
        body: JSON.stringify({
          task_id: 'card-1',
          operation: 'unlink_task',
        }),
      },
    );
    expect(mockApiClient.fetchJson).not.toHaveBeenCalledWith(
      '/specs/spec-1',
      expect.objectContaining({ method: 'PATCH' }),
    );
  });

  it('maps legacy collection field names to structured entity types', () => {
    expect(specEntityTypeForField('api_contracts')).toBe('api_contract');
    expect(specEntityTypeForField('technical_requirements')).toBe('technical_requirement');
    expect(specEntityTypeForField('observability_requirements')).toBe('observability_requirement');
  });
});
