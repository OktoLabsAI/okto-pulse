import { useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { useDashboardApi } from '@/services/api';
import { useCurrentBoard } from '@/store/dashboard';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import { PulseLoader } from '@/components/shared/PulseLoader';
import { isDeliverySummary, type DeliveryGroup, type DeliveryReason, type DeliverySummary, type DeliveryTaskGroup } from '@/types/delivery-summary';
import { TaskRequirementControl } from './TaskRequirementControl';
import type { DeliveryEvidencePanelProps } from './DeliveryEvidencePanel';

const labels: Record<DeliveryGroup, string> = { delivered: 'Delivered', in_progress: 'In progress', pending: 'Pending', optional_or_waived: 'Optional / Waived' };
const reasons: Record<DeliveryReason, string> = { delivered: 'Delivered', in_progress: 'Work in progress', not_started: 'Not started',
  awaiting_validation: 'Awaiting validation', paused: 'On hold', needs_rework: 'Rework required', cancelled: 'Cancelled',
  awaiting_dependency: 'Waiting for a dependency', awaiting_confirmation: 'Completion awaiting confirmation',
  updating: 'Completion awaiting an updated check', unavailable: 'Status not yet confirmed', optional: 'Optional', waived: 'Waived' };
const button = 'btn btn-secondary text-xs';
type Selection = Array<{ id: string; title: string }>;

export function DeliverySummaryPanel({ boardId, specId, revision, focusExceptionId, canManageTaskRequirements = false, renderDetails }:
  DeliveryEvidencePanelProps & { renderDetails: (active: boolean) => ReactNode }) {
  const api = useDashboardApi();
  const board = useCurrentBoard();
  const sectionId = useId();
  const [refresh, setRefresh] = useState(0);
  const owner = JSON.stringify([boardId, specId, revision, refresh]);
  const [stored, setStored] = useState<{ api: typeof api; owner: string; value: DeliverySummary }>();
  const summary = stored?.api === api && stored.owner === owner ? stored.value : undefined;
  const latest = useRef(summary);
  latest.current = summary;
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [detailsBlocked, setDetailsBlocked] = useState(false);
  const [pageError, setPageError] = useState('');
  const [busyGroup, setBusyGroup] = useState<DeliveryGroup>();
  const [optionalOpen, setOptionalOpen] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(Boolean(focusExceptionId));
  const [detailsMounted, setDetailsMounted] = useState(Boolean(focusExceptionId));
  const [selecting, setSelecting] = useState(false);
  const [selection, setSelection] = useState<Selection>([]);
  const [editing, setEditing] = useState<Selection>();
  const [editorVersion, setEditorVersion] = useState(0);
  const pageRequest = useRef<AbortController>();

  useEffect(() => {
    if (focusExceptionId) { setDetailsMounted(true); setDetailsOpen(true); }
  }, [focusExceptionId]);

  useEffect(() => {
    const reload = () => setRefresh(value => value + 1);
    const visible = () => { if (document.visibilityState === 'visible') reload(); };
    window.addEventListener('pulse:delivery-evidence-changed', reload);
    window.addEventListener('focus', reload);
    document.addEventListener('visibilitychange', visible);
    return () => {
      window.removeEventListener('pulse:delivery-evidence-changed', reload);
      window.removeEventListener('focus', reload);
      document.removeEventListener('visibilitychange', visible);
    };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    pageRequest.current?.abort(); setBusyGroup(undefined); setPageError('');
    setStored(undefined); setLoading(true); setError(''); setDetailsBlocked(false);
    // Initial read plus bounded backoff while the native worker is updating.
    // Focus, domain events and Refresh start a fresh read after this budget.
    void (async () => {
      for (const delay of [0, 1000, 2000, 5000]) {
        if (delay) await new Promise<void>(resolve => {
          const timer = setTimeout(resolve, delay);
          controller.signal.addEventListener('abort', () => { clearTimeout(timer); resolve(); }, { once: true });
        });
        if (controller.signal.aborted || delay && document.visibilityState !== 'visible') return;
        try {
          const value = await api.getDeliverySummary(boardId, specId, {}, controller.signal);
          if (!isDeliverySummary(value) || value.scope.board_id !== boardId || value.scope.spec_id !== specId)
            throw new Error('Delivery status could not be confirmed. Refresh to try again.');
          if (controller.signal.aborted) return;
          setStored({ api, owner, value }); setLoading(false); setError('');
          if (!['pending_initial', 'updating'].includes(value.projection.freshness)) return;
        } catch (failure) {
          if (controller.signal.aborted) return;
          setStored(undefined); setLoading(false);
          setDetailsBlocked(failure instanceof AuthenticatedFetchError && [401,403,404].includes(failure.status));
          setError(failure instanceof AuthenticatedFetchError && [401,403,404].includes(failure.status)
            ? 'Delivery status is unavailable with your current access.' : 'Delivery status could not be loaded. Refresh to try again.');
          return;
        }
      }
    })();
    return () => { controller.abort(); pageRequest.current?.abort(); };
  }, [api, boardId, specId, revision, refresh, owner]);

  async function loadGroup(group: DeliveryTaskGroup) {
    const base = latest.current;
    if (!base || pageRequest.current && !pageRequest.current.signal.aborted || group.loaded && !group.has_more) return;
    const controller = new AbortController(); pageRequest.current = controller;
    setBusyGroup(group.id); setPageError('');
    try {
      const value = await api.getDeliverySummary(boardId, specId,
        { group: group.id, limit: 10, ...(group.loaded ? { cursor: group.next_cursor! } : {}) }, controller.signal);
      if (controller.signal.aborted) return;
      if (!isDeliverySummary(value) || value.scope.board_id !== boardId || value.scope.spec_id !== specId)
        throw new Error('Invalid delivery page');
      if (latest.current?.population_id !== base.population_id || value.population_id !== base.population_id)
        throw new Error('Delivery population changed');
      if (JSON.stringify(value.scope) !== JSON.stringify(base.scope)
        || JSON.stringify(value.projection) !== JSON.stringify(base.projection)
        || value.complete !== base.complete || value.task_count !== base.task_count
        || value.required_task_count !== base.required_task_count || value.unique_obligation_count !== base.unique_obligation_count
        || value.groups.some((row,index) => row.count !== base.groups[index].count || row.id !== group.id && row.loaded))
        throw new Error('Delivery page changed its population');
      const next = value.groups.find(row => row.id === group.id)!;
      if (!next.loaded) throw new Error('Requested group was not loaded');
      const existingIds = new Set(base.groups.flatMap(row => row.items.map(task => task.card_id)));
      if (next.items.some(task => existingIds.has(task.card_id)) || next.has_more && next.next_cursor === group.next_cursor)
        throw new Error('Delivery page repeated');
      const merged = { ...base, groups: base.groups.map(row => row.id === group.id
        ? { ...next, items: [...row.items, ...next.items] } : row) };
      setStored({ api, owner, value: merged });
    } catch (failure) {
      if (controller.signal.aborted) return;
      if (failure instanceof AuthenticatedFetchError && [401,403,404].includes(failure.status)) {
        setStored(undefined); setDetailsBlocked(true); setError('Delivery status is unavailable with your current access.');
      } else setPageError('Tasks changed or this page is unavailable. Refresh the delivery status before continuing.');
    } finally {
      if (!controller.signal.aborted) setBusyGroup(undefined);
      if (pageRequest.current === controller) pageRequest.current = undefined;
    }
  }

  // Opening exceptions loads only that section. A complete zero count needs no GET.
  useEffect(() => {
    if (!optionalOpen || !summary) return;
    const group = summary.groups[3];
    if (!group.loaded && group.count !== 0 && !busyGroup && !pageError) void loadGroup(group);
  }, [optionalOpen, summary, busyGroup, pageError]);

  function edit(cards: Selection) {
    setEditing(cards); setEditorVersion(value => value + 1); setSelecting(false);
  }
  function responsible(id: string | null) {
    if (!id) return 'Unassigned';
    if (board?.id !== boardId) return 'Assigned';
    if (id === board.owner_id) return 'Board owner';
    return board.agents.find(agent => agent.id === id)?.name ?? 'Assigned';
  }
  function list(group: DeliveryTaskGroup) {
    return <div className="space-y-3">
      {!group.items.length && (group.count === 0 ? <p className="text-sm text-gray-500">No tasks.</p>
        : group.loaded ? <p className="text-sm text-gray-500">Status not yet confirmed.</p>
          : <p className="text-sm text-gray-500">{busyGroup === group.id ? 'Loading tasks…' : 'Task list is not loaded.'}</p>)}
      <ul className="space-y-2">
        {group.items.map(task => <li key={task.card_id} className="space-y-2 rounded-lg border border-gray-200 p-3 dark:border-gray-700">
          <div className="flex items-start gap-3">
            {selecting && <input type="checkbox" aria-label={`Select ${task.title}`}
              checked={selection.some(item => item.id === task.card_id)}
              disabled={selection.length >= 50 && !selection.some(item => item.id === task.card_id)}
              onChange={event => setSelection(items => event.target.checked
                ? [...items, { id: task.card_id, title: task.title }] : items.filter(item => item.id !== task.card_id))} />}
            <div className="min-w-0 flex-1 break-words"><p className="font-medium text-gray-900 dark:text-white">{task.title}</p>
              <p className="text-sm text-gray-600 dark:text-gray-300">{reasons[task.reason]}</p>
              {task.exception_applied && <p className="text-xs text-amber-700 dark:text-amber-300">Exception applied</p>}
              <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">{responsible(task.assignee_id)}
                {task.updated_at && <> · Updated <time dateTime={task.updated_at}>{new Date(task.updated_at).toLocaleString()}</time></>}</p>
            </div>
          </div>
          <button type="button" className={button} onClick={() => edit([{ id: task.card_id, title: task.title }])}>
            {canManageTaskRequirements ? 'Change requirement' : 'View requirement'}<span className="sr-only"> for {task.title}</span>
          </button>
        </li>)}
      </ul>
      {group.has_more && <button type="button" className={button} disabled={Boolean(busyGroup)} onClick={() => void loadGroup(group)}>
        {busyGroup === group.id ? 'Loading…' : `Load more ${labels[group.id].toLowerCase()} tasks`}
      </button>}
    </div>;
  }

  return <section aria-label="Delivery status" className="space-y-5">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div><h3 className="text-lg font-semibold text-gray-900 dark:text-white">Delivery</h3>
        <p className="text-sm text-gray-500 dark:text-gray-400">What has been delivered, what is in progress and what is pending.</p></div>
      <button type="button" className={button} disabled={loading} onClick={() => setRefresh(value => value + 1)}>Refresh delivery status</button>
    </div>
    {loading && <PulseLoader label="Loading delivery status…" size="sm" />}
    {error && <p role="alert" className="text-sm text-amber-700 dark:text-amber-300">{error}</p>}
    {editing && <div hidden={!summary || Boolean(error)}>
      <TaskRequirementControl key={editorVersion} boardId={boardId} specId={specId}
        cards={editing} initiallyOpen initiallySelected active={Boolean(summary) && !error}
        canEdit={canManageTaskRequirements} revision={revision}
        onChanged={() => setRefresh(value => value + 1)} />
    </div>}
    {summary && <>
      {!summary.complete && <p role="status" className="text-sm text-gray-500 dark:text-gray-400">
        {summary.projection.freshness === 'pending_initial' ? 'Delivery status is being prepared.'
          : summary.projection.freshness === 'unavailable' ? 'Delivery status could not be confirmed.'
            : 'Delivery status is updating. Completion and task counts are not yet confirmed.'} Refresh in a moment.
      </p>}
      {summary.complete && summary.task_count === 0 && <p>No tasks in this Spec.</p>}
      <button type="button" className={button} aria-pressed={selecting} onClick={() => setSelecting(value => !value)}>
        {canManageTaskRequirements ? 'Change task requirements' : 'View task requirements'}
      </button>
      {selecting && <div className="space-y-2">
        <p className="text-sm">Select up to 50 tasks. Load more to find another task.</p>
        <button type="button" className={button} disabled={!selection.length} onClick={() => edit(selection)}>
          Continue with {selection.length} selected
        </button>
      </div>}
      <div className="grid gap-4 lg:grid-cols-3">
        {summary.groups.slice(0,3).map(group => <section key={group.id} aria-label={labels[group.id]}
          className="min-w-0 space-y-3 rounded-xl bg-gray-50 p-3 dark:bg-gray-800/40">
          <h4 className="font-semibold text-gray-800 dark:text-gray-100">{labels[group.id]}{group.count !== null && ` (${group.count})`}</h4>
          {list(group)}
        </section>)}
      </div>
      <details open={optionalOpen} onToggle={event => setOptionalOpen(event.currentTarget.open)}
        className="rounded-lg border border-gray-200 p-3 dark:border-gray-700">
        <summary className="cursor-pointer rounded font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-violet-500">
          Optional / Waived{summary.groups[3].count !== null && ` (${summary.groups[3].count})`}
        </summary>
        {optionalOpen && <div className="mt-3 space-y-3"><p className="text-sm text-gray-500">Not required for delivery. This does not count as delivered work.</p>{list(summary.groups[3])}</div>}
      </details>
      {pageError && <p role="alert" className="text-sm text-amber-700 dark:text-amber-300">{pageError}</p>}
    </>}
    <button type="button" className={button} aria-expanded={detailsOpen} aria-controls={`${sectionId}-details`}
      onClick={() => { setDetailsMounted(true); setDetailsOpen(value => !value); }}>{detailsOpen ? 'Hide Details' : 'Details'}</button>
    {detailsMounted && <div id={`${sectionId}-details`} hidden={!detailsOpen || detailsBlocked}>
      {renderDetails(detailsOpen && !detailsBlocked)}
    </div>}
  </section>;
}
