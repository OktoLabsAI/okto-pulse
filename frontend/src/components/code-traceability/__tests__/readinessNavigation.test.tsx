import { useRef } from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, cleanup, render, renderHook, screen, waitFor } from '@testing-library/react';
import { ModalStackProvider, useModalStack } from '@/contexts/ModalStackContext';
import { useNativeReferenceFocus } from '@/components/shared/useNativeReferenceFocus';
import { TextRequirementsTab } from '@/components/specs/TextRequirementsTab';
import { readinessSpecLocation } from '../readinessNavigation';
import { useNativeReadinessNavigation } from '../useNativeReadinessNavigation';

const api = vi.hoisted(() => ({ getCard: vi.fn(), getSpec: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
beforeEach(() => api.getSpec.mockResolvedValue({ id: 's', board_id: 'b' }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

it.each([['fr', 'frs'], ['ac', 'acs'], ['br', 'rules'], ['api', 'contracts'], ['ir', 'irs'],
  ['or', 'ors'], ['tr', 'trs'], ['decision', 'decisions']])('routes canonical %s references without matching free text', (kind, tab) => {
  expect(readinessSpecLocation({ type: 'obligation', id: `${kind}:identity` }, 's')).toEqual({ tab, ref: `${kind}:identity` });
  expect(readinessSpecLocation({ type: 'obligation', id: 'identity' }, 's')).toBeNull();
});

it('keeps same-Spec navigation local and does not turn unsupported references into links', async () => {
  const open = vi.fn();
  const { result } = renderHook(() => useNativeReadinessNavigation('b', 's', open));
  await act(async () => result.current.navigator.open({ type: 'scenario', id: 'scenario-1' }));
  expect(open).toHaveBeenCalledWith({ tab: 'tests', ref: 'scenario:scenario-1' });
  expect(result.current.navigator.supports!({ type: 'obligation', id: 'untyped' })).toBe(false);
  expect(result.current.navigator.supports!({ type: 'spec', id: 'other-spec' })).toBe(false);
  expect(result.current.navigator.supports!({ type: 'record', id: 'record-1' })).toBe(false);
});

it('opens a Card only after native read authorization and exact Board/Spec ownership verification', async () => {
  api.getCard.mockResolvedValue({ id: 'test', board_id: 'b', spec_id: 's' });
  const { result } = renderHook(() => ({ nav: useNativeReadinessNavigation('b', 's'), stack: useModalStack() }), { wrapper: ModalStackProvider });
  await act(async () => result.current.nav.navigator.open({ type: 'card', id: 'test' }));
  expect(api.getCard).toHaveBeenCalledWith('test');
  expect(result.current.stack.stack).toEqual([{ type: 'card', id: 'test', boardId: 'b' }]);
  api.getCard.mockResolvedValue({ id: 'foreign', board_id: 'b', spec_id: 'different' });
  await act(async () => result.current.nav.navigator.open({ type: 'card', id: 'foreign' }));
  expect(result.current.stack.stack).toHaveLength(1);
  expect(result.current.nav.error).toMatch(/no longer belongs/);
});

it('ignores a late Card read after changing diagnostic scope and preserves denial', async () => {
  let finish!: (card: object) => void;
  api.getCard.mockReturnValue(new Promise(resolve => { finish = resolve; }));
  const { result, rerender } = renderHook(({ spec }) => ({ nav: useNativeReadinessNavigation('b', spec), stack: useModalStack() }),
    { initialProps: { spec: 's' }, wrapper: ModalStackProvider });
  act(() => { result.current.nav.navigator.open({ type: 'card', id: 'test' }); });
  rerender({ spec: 'new' });
  await act(async () => finish({ id: 'test', board_id: 'b', spec_id: 's' }));
  expect(result.current.stack.stack).toEqual([]);
  api.getCard.mockRejectedValue(new Error('Permission denied'));
  await act(async () => result.current.nav.navigator.open({ type: 'card', id: 'test' }));
  expect(result.current.nav.error).toBe('Permission denied');
  expect(result.current.stack.stack).toEqual([]);
});

it('opens the scoped native Spec section and updates a same-entity route without adding layers', async () => {
  const { result } = renderHook(() => ({ nav: useNativeReadinessNavigation('b', 's'), stack: useModalStack() }), { wrapper: ModalStackProvider });
  await act(async () => result.current.nav.navigator.open({ type: 'obligation', id: 'ac:capacity' }));
  expect(result.current.stack.stack).toEqual([{ type: 'spec', id: 's', boardId: 'b', initialTab: 'acs', focusReadinessRef: 'ac:capacity' }]);
  await act(async () => result.current.nav.navigator.open({ type: 'obligation', id: 'fr:capacity' }));
  expect(result.current.stack.stack).toEqual([{ type: 'spec', id: 's', boardId: 'b', initialTab: 'frs', focusReadinessRef: 'fr:capacity' }]);
});

it('does not navigate a Spec after an ownership change or a denied native read', async () => {
  const open = vi.fn();
  const { result } = renderHook(() => useNativeReadinessNavigation('b', 's', open));
  api.getSpec.mockResolvedValueOnce({ id: 's', board_id: 'different' });
  await act(async () => result.current.navigator.open({ type: 'obligation', id: 'ac:capacity' }));
  expect(result.current.error).toMatch(/no longer belongs/);
  expect(open).not.toHaveBeenCalled();
  api.getSpec.mockRejectedValueOnce(new Error('Permission denied'));
  await act(async () => result.current.navigator.open({ type: 'scenario', id: 'scenario-1' }));
  expect(result.current.error).toBe('Permission denied');
  expect(open).not.toHaveBeenCalled();
});

it('focuses and expands the exact native row when it arrives, without editing or touching another modal', async () => {
  const save = vi.fn(), revoke = vi.fn();
  function Native({ loaded }: { loaded: boolean }) {
    const root = useRef<HTMLDivElement>(null);
    useNativeReferenceFocus(root, 'ac:capacity', 'b:s');
    return <><div data-readiness-ref="ac:capacity" tabIndex={-1}>Another modal</div><div ref={root}>
      {loaded && <TextRequirementsTab kind="AC" items={[{ id: 'capacity', title: 'Capacity', text: 'Reject overbooking', status: 'active' }]}
        canCreate canEdit canRevoke onSave={save} onRevoke={revoke} />}
    </div></>;
  }
  const view = render(<Native loaded={false} />);
  view.rerender(<Native loaded />);
  const row = screen.getByRole('article');
  await waitFor(() => expect(row).toHaveFocus());
  expect(screen.getByText('Reject overbooking')).toBeVisible();
  expect(screen.getByText('Another modal')).not.toHaveFocus();
  expect(save).not.toHaveBeenCalled(); expect(revoke).not.toHaveBeenCalled();
});
