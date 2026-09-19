import { renderHook } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { useDashboardApi } from '../api';
const client = vi.hoisted(() => ({ fetchJson: vi.fn() }));
vi.mock('@/contexts/ApiContext', () => ({ useApiClient: () => client }));

it('updates one scenario method with an exact version and no hidden retry', async () => {
  client.fetchJson.mockClear();
  const { result } = renderHook(() => useDashboardApi());
  await result.current.updateScenarioVerificationMethod('board/a', 'spec b', 'scenario/a', 'inspection', 9);
  expect(client.fetchJson).toHaveBeenCalledExactlyOnceWith('/boards/board%2Fa/specs/spec%20b/scenarios/scenario%2Fa/verification-method', {
    method: 'PATCH', body: JSON.stringify({ verification_method: 'inspection', expected_spec_version: 9 }), maxRetries: 0,
  });
  client.fetchJson.mockClear();
});

it('encodes exact requirement identity and path window with cancellation on the authorized read', async () => {
  const { result } = renderHook(() => useDashboardApi());
  const controller = new AbortController();
  await result.current.getRequirementVerification('board/a', 'spec b', controller.signal, { requirementType: 'business_rule', requirementId: 'br/a & b', pathsOffset: 100, limit: 1 });
  const [path, options] = client.fetchJson.mock.calls[0];
  const url = new URL(path, 'https://pulse.invalid');
  expect(url.pathname).toBe('/boards/board%2Fa/specs/spec%20b/requirement-verification');
  expect(Object.fromEntries(url.searchParams)).toEqual({ requirement_type: 'business_rule', requirement_id: 'br/a & b', paths_offset: '100', limit: '1' });
  expect(options).toEqual({ signal: controller.signal });
});
