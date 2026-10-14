import { useRef } from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, cleanup, render, renderHook, screen, waitFor } from '@testing-library/react';
import { ModalStackProvider, useModalStack } from '@/contexts/ModalStackContext';
import { useNativeReferenceFocus } from '@/components/shared/useNativeReferenceFocus';
import { TextRequirementsTab } from '@/components/specs/TextRequirementsTab';
import { readinessSpecLocation } from '../readinessNavigation';
import { useNativeReadinessNavigation } from '../useNativeReadinessNavigation';

const api = vi.hoisted(() => ({ getCard: vi.fn(), getSpec: vi.fn(), getDeliveryEvidence: vi.fn(), getCodeTraceabilityProjection: vi.fn(), getDecisionReviews: vi.fn() }));
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

it('opens the exact native inspection batch, preserving prior editions and all its Decisions', async () => {
  const open = vi.fn();
  api.getDecisionReviews.mockResolvedValue({ board_id: 'b', spec_id: 's', edition: 2,
    history: [{ id: 'review', edition: 1, observations: [{ decision_id: 'a' }, { decision_id: 'b' }] }] });
  const { result } = renderHook(() => useNativeReadinessNavigation('b', 's', open));
  expect(result.current.navigator.supports!({ type: 'inspection', id: 'review' })).toBe(true);
  await act(async () => result.current.navigator.open({ type: 'inspection', id: 'review' }));
  expect(open).toHaveBeenCalledWith({ tab: 'decisions', ref: 'inspection:review' });
  expect(api.getDecisionReviews).toHaveBeenCalledWith('b', 's');
});

it.each(['foreign', 'missing', 'ambiguous', 'denied'])('refuses %s inspection navigation', async kind => {
  const open = vi.fn();
  if (kind === 'denied') api.getDecisionReviews.mockRejectedValueOnce(new Error('Permission denied'));
  else api.getDecisionReviews.mockResolvedValueOnce({ board_id: kind === 'foreign' ? 'other' : 'b', spec_id: 's',
    history: kind === 'missing' ? [] : kind === 'ambiguous' ? [{ id: 'review' }, { id: 'review' }] : [{ id: 'review' }] });
  const { result } = renderHook(() => useNativeReadinessNavigation('b', 's', open));
  await act(async () => result.current.navigator.open({ type: 'inspection', id: 'review' }));
  expect(open).not.toHaveBeenCalled();
  expect(result.current.error).toMatch(/unavailable|denied/);
});

it('ignores an inspection read completed after the diagnosis scope changes', async () => {
  const open = vi.fn();
  let finish!: (data: object) => void;
  api.getDecisionReviews.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
  const { result, rerender } = renderHook(({ spec }) => useNativeReadinessNavigation('b', spec, open), { initialProps: { spec: 's' } });
  act(() => { result.current.navigator.open({ type: 'inspection', id: 'review' }); });
  await waitFor(() => expect(api.getDecisionReviews).toHaveBeenCalled());
  rerender({ spec: 'new' });
  await act(async () => finish({ board_id: 'b', spec_id: 's', history: [{ id: 'review' }] }));
  expect(open).not.toHaveBeenCalled();
});

it('opens a test receipt only by exact opaque identity on the native scenario', async () => {
  const open = vi.fn();
  api.getSpec.mockResolvedValueOnce({ id: 's', board_id: 'b', test_scenarios: [{ id: 'scenario', evidence: { execution_receipt: 'opaque.receipt' } }] });
  const { result } = renderHook(() => useNativeReadinessNavigation('b', 's', open));
  await act(async () => result.current.navigator.open({ type: 'receipt', id: 'opaque.receipt', scenarioId: 'scenario' }));
  expect(open).toHaveBeenCalledWith({ tab: 'tests', ref: 'receipt:opaque.receipt' });
  expect(api.getDeliveryEvidence).not.toHaveBeenCalled();
});

it('opens a Spec-owned exception in Coverage, never in a guessed Card ledger', async () => {
  const open = vi.fn();
  api.getDeliveryEvidence.mockResolvedValueOnce({ board_id: 'b', spec_id: 's', records: [{ id: 'waiver', kind: 'waiver' }] });
  const { result } = renderHook(() => useNativeReadinessNavigation('b', 's', open));
  await act(async () => result.current.navigator.open({ type: 'exception', id: 'waiver' }));
  expect(open).toHaveBeenCalledWith({ tab: 'coverage', ref: 'exception:waiver' });
  expect(api.getCard).not.toHaveBeenCalled();
});

it.each(['foreign', 'missing', 'wrong-kind', 'ambiguous', 'denied'])('refuses %s exception navigation', async kind => {
  const open = vi.fn();
  if (kind === 'denied') api.getDeliveryEvidence.mockRejectedValueOnce(new Error('Permission denied'));
  else api.getDeliveryEvidence.mockResolvedValueOnce({ board_id: kind === 'foreign' ? 'other' : 'b', spec_id: 's',
    records: kind === 'missing' ? [] : kind === 'ambiguous' ? [{ id: 'waiver', kind: 'waiver' }, { id: 'waiver', kind: 'waiver' }]
      : [{ id: 'waiver', kind: kind === 'wrong-kind' ? 'implementation' : 'waiver' }] });
  const { result } = renderHook(() => useNativeReadinessNavigation('b', 's', open));
  await act(async () => result.current.navigator.open({ type: 'exception', id: 'waiver' }));
  expect(open).not.toHaveBeenCalled();
  expect(result.current.error).toMatch(/unavailable|denied/);
});

it.each(['replaced', 'wrong-scenario', 'ambiguous'])('does not substitute %s evidence for a receipt', async kind => {
  const open = vi.fn();
  const scenario = { id: 'scenario', evidence: { execution_receipt: kind === 'replaced' ? 'new-receipt' : 'receipt' } };
  api.getSpec.mockResolvedValueOnce({ id: 's', board_id: 'b', test_scenarios: kind === 'ambiguous' ? [scenario, { ...scenario, id: 'second' }] : [scenario] });
  const { result } = renderHook(() => useNativeReadinessNavigation('b', 's', open));
  await act(async () => result.current.navigator.open({ type: 'receipt', id: 'receipt', scenarioId: kind === 'wrong-scenario' ? 'foreign' : 'scenario' }));
  expect(open).not.toHaveBeenCalled();
  expect(result.current.error).toMatch(/unavailable or ambiguous/);
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

it.each(['target', 'execution'] as const)('opens a scoped native %s only after checking its Card projection', async type => {
  api.getCard.mockResolvedValue({ id: 'c', board_id: 'b', spec_id: 's', card_type: 'normal', subject_version: 3 });
  api.getCodeTraceabilityProjection.mockResolvedValue({ subject_type: 'card', subject_id: 'c', targets: [{ id: 't' }], executions: [{ id: 'e' }] });
  const { result } = renderHook(() => ({ nav: useNativeReadinessNavigation('b', 's'), stack: useModalStack() }), { wrapper: ModalStackProvider });
  const id = type === 'target' ? 't' : 'e';
  await act(async () => result.current.nav.navigator.open({ type, id, cardId: 'c' }));
  expect(api.getCodeTraceabilityProjection).toHaveBeenCalledWith('b', 'card', 'c', 3);
  expect(result.current.stack.stack).toEqual([{ type: 'card', id: 'c', boardId: 'b', target: {
    tab: 'implementation-targets', focusDiagnosis: false, focusReference: `${type}:${id}`,
  } }]);
  api.getCodeTraceabilityProjection.mockResolvedValueOnce({ subject_type: 'card', subject_id: 'foreign', targets: [], executions: [] });
  await act(async () => result.current.nav.navigator.open({ type, id: 'missing', cardId: 'c' }));
  expect(result.current.nav.error).toMatch(/unavailable in this Card scope/);
  expect(result.current.stack.stack).toHaveLength(1);
});

it('resolves record ownership from native evidence, refuses ambiguity, and handles fallback Card obligations', async () => {
  api.getCard.mockResolvedValue({ id: 'c', board_id: 'b', spec_id: 's', card_type: 'normal' });
  api.getDeliveryEvidence.mockResolvedValue({ board_id: 'b', spec_id: 's', records: [{ id: 'r', payload: { card_id: 'c' } }], implementations: [], tests: [] });
  const { result } = renderHook(() => ({ nav: useNativeReadinessNavigation('b', 's'), stack: useModalStack() }), { wrapper: ModalStackProvider });
  await act(async () => result.current.nav.navigator.open({ type: 'record', id: 'r' }));
  expect(result.current.stack.stack).toEqual([{ type: 'card', id: 'c', boardId: 'b', target: {
    tab: 'delivery', focusDiagnosis: false, focusReference: 'record:r', recordId: 'r',
  } }]);
  api.getDeliveryEvidence.mockResolvedValueOnce({ board_id: 'b', spec_id: 's', records: [{ id: 'r', payload: { card_id: 'foreign' } }],
    implementations: [{ id: 'r', card_id: 'c' }], tests: [] });
  await act(async () => result.current.nav.navigator.open({ type: 'record', id: 'r' }));
  expect(result.current.nav.error).toMatch(/ambiguous/);
  expect(api.getCard).toHaveBeenCalledTimes(1);
  await act(async () => result.current.nav.navigator.open({ type: 'obligation', id: 'card:c' }));
  expect(api.getCard).toHaveBeenCalledTimes(2);
});
