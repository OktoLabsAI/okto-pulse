import { useEffect, useRef, useState } from 'react';
import { useDashboardApi } from '@/services/api';
import { isReadinessResponse, type PersistedReadinessProjection, type ReadinessResponse } from '@/types/delivery-readiness';
import { DeliveryReadinessView } from './DeliveryReadinessView';
import { DeliveryReadinessIssues } from './DeliveryReadinessIssues';
import type { ReadinessCardLocation, ReadinessNavigator, ReadinessSpecLocation } from './readinessNavigation';
import { useNativeReadinessNavigation } from './useNativeReadinessNavigation';
import { useDeliveryReadiness } from './useDeliveryReadiness';
import { readinessReadMode } from '@/lib/readinessMode';

export function DeliveryReadinessPanel({ boardId, specId, cardId, revision, active = true, focusDiagnosis = false, persistedValue, onNavigate, onOpenReadinessSpec, onOpenReadinessCard }: {
  boardId: string; specId: string; cardId?: string; revision?: string | number;
  focusDiagnosis?: boolean; onNavigate?: ReadinessNavigator;
  active?: boolean;
  persistedValue?: PersistedReadinessProjection | null;
  onOpenReadinessSpec?: (location: ReadinessSpecLocation) => void;
  onOpenReadinessCard?: (id: string, target?: ReadinessCardLocation) => void;
}) {
  const api = useDashboardApi();
  const navigation = useNativeReadinessNavigation(boardId, specId, onOpenReadinessSpec, onOpenReadinessCard);
  const [refresh, setRefresh] = useState(0);
  const [expanded, setExpanded] = useState(focusDiagnosis);
  const persistedSummary = persistedValue?.summaries.find(row => row.card_id === (cardId ?? null));
  const inheritedProjection = persistedValue ? { summary: persistedSummary,
    freshness: persistedSummary ? persistedValue.projection.freshness : 'pending_initial' as const,
    generationId: persistedValue.projection.generation_id,
    errorCode: persistedValue.projection.error_code } : undefined;
  const state = useDeliveryReadiness(boardId, specId, cardId, JSON.stringify([revision, refresh]),
    active && (expanded || !persistedValue), expanded || readinessReadMode() === 'legacy' ? 'diagnostic' : 'projection');
  const initial = isReadinessResponse(state.value) ? state.value : undefined;
  const key = JSON.stringify([boardId, specId, cardId, initial?.snapshot_id, revision, refresh, state.loading]);
  const [extra, setExtra] = useState<{ key: string; value: ReadinessResponse }>();
  const [failure, setFailure] = useState<{ key: string; message: string }>();
  const [busy, setBusy] = useState(false);
  const [openCauses, setOpenCauses] = useState<{ scope: string; snapshot: string; ids: string[] }>();
  const scopeKey = JSON.stringify([boardId, specId, cardId]);
  const root = useRef<HTMLDivElement>(null);
  const focusPending = useRef(focusDiagnosis);
  const focusedControl = useRef<{ scope: string; snapshot: string; key: string }>();
  useEffect(() => { focusPending.current = focusDiagnosis; focusedControl.current = undefined; }, [scopeKey, focusDiagnosis]);
  const heading = useRef<HTMLHeadingElement>(null);
  const origin = useRef<HTMLElement | null>(null);
  const request = useRef<AbortController>();
  const currentKey = useRef(key); currentKey.current = key;
  useEffect(() => {
    request.current?.abort(); setBusy(false);
    return () => request.current?.abort();
  }, [key, active]);
  const value = extra?.key === key ? extra.value : initial;
  const error = failure?.key === key ? failure.message : state.error;
  const unavailable = !state.loading && !state.error && state.value && !initial
    ? 'Readiness diagnostic contract unavailable or incompatible.' : undefined;
  const snapshot = value?.snapshot_id;
  useEffect(() => {
    if (!active || !expanded || !snapshot || error) return;
    if (focusPending.current) {
      heading.current?.focus(); focusPending.current = false;
    } else if (focusedControl.current?.scope === scopeKey && focusedControl.current.snapshot === snapshot
      && document.activeElement === document.body) {
      Array.from(root.current?.querySelectorAll<HTMLElement>('[data-readiness-focus]') ?? [])
        .find(element => element.dataset.readinessFocus === focusedControl.current?.key)?.focus();
    }
  }, [active, expanded, focusDiagnosis, snapshot, error, scopeKey]);
  function inspect() {
    origin.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    focusPending.current = true;
    setExpanded(true); heading.current?.focus();
  }
  async function more() {
    if (!active || !value?.page.next_cursor || busy) return;
    const controller = new AbortController(); request.current = controller;
    const expected = value, expectedKey = key;
    setBusy(true);
    try {
      const page = await api.getDeliveryReadinessPage(boardId, specId, {
        card_id: cardId, snapshot_id: expected.snapshot_id, cursor: expected.page.next_cursor!, limit: 20,
      }, controller.signal);
      if (!isReadinessResponse(page) || page.snapshot_id !== expected.snapshot_id
        || page.scope.board_id !== boardId || page.scope.spec_id !== specId || page.scope.card_id !== (cardId ?? null))
        throw new Error('Readiness scope or snapshot changed. Refresh the assessment.');
      if (!controller.signal.aborted && currentKey.current === expectedKey) setExtra({ key, value: {
        ...page, issues: [...expected.issues, ...page.issues.filter(issue => !expected.issues.some(old => old.id === issue.id))],
        next_actions: page.next_actions,
      } });
    } catch (e) {
      if (!controller.signal.aborted && currentKey.current === expectedKey) setFailure({ key, message: e instanceof Error ? e.message : 'Readiness page unavailable. Refresh to retry.' });
    } finally { if (!controller.signal.aborted && currentKey.current === expectedKey) setBusy(false); }
  }
  return <div ref={root} className="min-w-0 space-y-4" onFocusCapture={event => {
    const element = event.target instanceof HTMLElement ? event.target.closest<HTMLElement>('[data-readiness-focus]') : null;
    focusedControl.current = element?.dataset.readinessFocus && snapshot
      ? { scope: scopeKey, snapshot, key: element.dataset.readinessFocus } : undefined;
  }}>
    <DeliveryReadinessView value={error || unavailable ? undefined : value} persisted={state.persisted ?? inheritedProjection}
      loading={state.loading} error={error || unavailable}
      onInspect={inspect} onRefresh={() => setRefresh(v => v + 1)} />
    {expanded && !state.loading && !error && !unavailable && value && <section aria-label="Delivery diagnosis" className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 ref={heading} tabIndex={-1} className="rounded text-sm font-semibold outline-offset-4">{value.delivery_state === 'not_applicable' ? 'Scope details' : 'What is missing?'}</h3>
        <button type="button" onClick={() => { setExpanded(false); origin.current?.focus(); }} className="rounded border px-3 py-1.5 text-xs dark:border-gray-600">Hide diagnosis</button>
      </div>
      {navigation.error && <p role="alert" className="text-sm text-red-700 dark:text-red-300">{navigation.error}</p>}
      <DeliveryReadinessIssues value={value} onNavigate={onNavigate ?? navigation.navigator} onSnapshotInvalid={message => setFailure({ key, message })}
        expandedIssueIds={openCauses?.scope === scopeKey && openCauses.snapshot === value.snapshot_id ? openCauses.ids : []}
        onIssueToggle={(id, open) => setOpenCauses(previous => {
          const ids = previous?.scope === scopeKey && previous.snapshot === value.snapshot_id ? previous.ids : [];
          if (ids.includes(id) === open) return previous;
          return { scope: scopeKey, snapshot: value.snapshot_id, ids: open ? [...ids, id] : ids.filter(item => item !== id) };
        })} />
      {value.page.has_more && <button type="button" disabled={busy} onClick={() => void more()} className="rounded border px-3 py-2 text-xs dark:border-gray-600">
        {busy ? 'Loading causes…' : 'Load more causes'}
      </button>}
    </section>}
  </div>;
}
