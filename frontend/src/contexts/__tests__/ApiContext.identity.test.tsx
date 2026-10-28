import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render } from '@testing-library/react';
import { ApiProvider, useApiClient } from '../ApiContext';

const auth = vi.hoisted(() => ({
  identityKey: 'realm-a/actor-a',
  getToken: async () => null as string | null,
}));
vi.mock('@/adapters', () => ({ authAdapter: { useAuth: () => ({
  isLoaded: true, isSignedIn: true, identityKey: auth.identityKey, getToken: auth.getToken,
}) } }));
afterEach(() => { cleanup(); auth.identityKey = 'realm-a/actor-a'; });

it('keeps the API client stable for one actor and replaces it across realm or actor changes', () => {
  const seen: ReturnType<typeof useApiClient>[] = [];
  function Probe() { seen.push(useApiClient()); return null; }
  const view = render(<ApiProvider><Probe /></ApiProvider>);
  const first = seen.at(-1);
  view.rerender(<ApiProvider><Probe /></ApiProvider>);
  expect(seen.at(-1)).toBe(first);
  auth.identityKey = 'realm-a/actor-b';
  view.rerender(<ApiProvider><Probe /></ApiProvider>);
  const second = seen.at(-1);
  expect(second).not.toBe(first);
  auth.identityKey = 'realm-b/actor-b';
  view.rerender(<ApiProvider><Probe /></ApiProvider>);
  expect(seen.at(-1)).not.toBe(second);
});
