import { useEffect, useId, useRef, useState } from 'react';
import { useDashboardApi } from '@/services/api';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import { ContextualHelpLink } from '@/components/help/ContextualHelpLink';
import type { TaskRequirementChoice, TaskRequirementInput, TaskRequirementOptions } from '@/types/task-requirement';
import { TaskRequirementHistory } from './TaskRequirementHistory';
import { requirementLabels } from './taskRequirementLabels';

interface Props {
  boardId: string; specId: string; cards: Array<{ id: string; title: string }>;
  active?: boolean; canEdit?: boolean; initiallyOpen?: boolean; revision?: string | number; onChanged?: () => void;
}

// Mount with a board/spec key. Options are fetched only while editing an exact
// selection; a changed server basis never replaces a human draft silently.
export function TaskRequirementControl({ boardId, specId, cards, active = true, canEdit = true, initiallyOpen = false, revision, onChanged }: Props) {
  const api = useDashboardApi();
  const headingId = useId();
  const [open, setOpen] = useState(initiallyOpen);
  const [selected, setSelected] = useState<string[]>(cards.length === 1 ? [cards[0].id] : []);
  const [choice, setChoice] = useState<TaskRequirementChoice | ''>('');
  const [note, setNote] = useState('');
  const [options, setOptions] = useState<TaskRequirementOptions | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [refresh, setRefresh] = useState(0);
  const [stale, setStale] = useState(false);
  const [uncertain, setUncertain] = useState<TaskRequirementInput | null>(null);
  const [requestedRevision, setRequestedRevision] = useState(revision);
  const [historyOpen, setHistoryOpen] = useState<Record<string, boolean>>({});
  const alive = useRef(true);
  const selectionKey = JSON.stringify(selected);
  const basisChanged = requestedRevision !== revision;

  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => {
    if (!open || !active || !selected.length || basisChanged) return;
    const controller = new AbortController();
    setLoading(true); setOptions(null); setError(''); setStale(false);
    api.getTaskRequirementOptions(boardId, specId, selected, controller.signal).then(value => {
      if (controller.signal.aborted) return;
      if (value.contract_version !== 'task-requirement-options/v1'
        || value.board_id !== boardId || value.spec_id !== specId
        || JSON.stringify(value.cards.map(card => card.card_id).sort()) !== JSON.stringify([...selected].sort())) {
        throw new Error('Current task requirements could not be confirmed.');
      }
      setOptions(value);
    }).catch(err => {
      if (!controller.signal.aborted) setError(err instanceof Error ? err.message : 'Task requirements could not be loaded.');
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  // Selection identity is a value, rather than the caller's array identity.
  }, [api, boardId, specId, open, active, selectionKey, requestedRevision, basisChanged, refresh]);

  async function save() {
    if (busy || !active) return;
    if (!uncertain && (!canEdit || !options?.can_apply || loading || stale || basisChanged || !choice || !options.allowed_decisions.includes(choice))) return;
    const request: TaskRequirementInput = uncertain ?? {
      contract_version: 'task-requirement-decision/v1', card_ids: [...selected],
      decision: choice as TaskRequirementChoice, basis: options!.basis,
      note: note.trim() || null, idempotency_key: crypto.randomUUID(),
    };
    setBusy(true); setError('');
    try {
      const result = await api.applyTaskRequirementDecision(boardId, specId, request);
      if (result.contract_version !== 'task-requirement-operation/v1' || result.status !== 'applied'
        || result.decision !== request.decision
        || JSON.stringify([...result.affected_cards].sort()) !== JSON.stringify([...request.card_ids].sort())) {
        throw new Error('The decision response could not be confirmed.');
      }
      if (!alive.current) return;
      setUncertain(null); setOptions(null); setOpen(false); setChoice(''); setNote('');
      setNotice(result.replayed ? 'The original decision was saved. Review current requirements before making another change.' : 'Task requirement saved. Work status and proof are unchanged.');
      onChanged?.();
    } catch (err) {
      if (!alive.current) return;
      if (err instanceof AuthenticatedFetchError && [400, 401, 403, 404, 409, 422].includes(err.status)) {
        setUncertain(null); setStale(true);
        if ([401, 403, 404].includes(err.status)) setOptions(null);
        setError(err.status === 409 ? 'The task requirements changed or this draft conflicts with an earlier operation. Review the latest requirements; your note and choice are preserved.' : err.message);
      } else {
        setUncertain(request);
        setError('The response is uncertain. Retry the same decision before making another change.');
      }
    } finally { if (alive.current) setBusy(false); }
  }

  const locked = busy || Boolean(uncertain);
  return <section aria-label="Task requirement" className="space-y-3 rounded-lg border border-gray-200 p-4 dark:border-gray-700">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <button type="button" className="btn btn-secondary" aria-expanded={open} aria-controls={headingId}
        disabled={!active || locked} onClick={() => { setOpen(value => !value); setRequestedRevision(revision); setNotice(''); }}>
        {open ? 'Close task requirement' : canEdit ? 'Change task requirement' : 'View task requirement'}
      </button>
      <ContextualHelpLink sectionId="task-requirements">Task requirement rules</ContextualHelpLink>
    </div>
    {notice && <p role="status" className="text-sm">{notice}</p>}
    {open && <form id={headingId} className="space-y-3" onSubmit={event => { event.preventDefault(); void save(); }}>
      <p className="text-sm text-gray-600 dark:text-gray-300">{canEdit ? 'Choose Required, Optional or Waived. A note is optional. ' : ''}This is the delivery requirement for this Spec edition; work status and evidence stay unchanged.</p>
      <fieldset disabled={locked || !active} className="space-y-2">
        <legend className="text-sm font-medium">Tasks (up to 50)</legend>
        {cards.map(card => {
          const current = options?.cards.find(row => row.card_id === card.id);
          return <div key={card.id} className="space-y-1">
          <label className="flex items-start gap-2 text-sm">
          <input type="checkbox" aria-label={card.title} checked={selected.includes(card.id)} disabled={!selected.includes(card.id) && selected.length >= 50}
            onChange={event => { setSelected(ids => event.target.checked ? [...ids, card.id] : ids.filter(id => id !== card.id)); setRequestedRevision(revision); setOptions(null); setNotice(''); }} />
          <span className="min-w-0 flex-1">{card.title}</span>
          {current && <span className="rounded bg-gray-100 px-2 py-0.5 text-xs dark:bg-gray-800">{requirementLabels[current.decision]}</span>}
          </label>
          {current && <div className="ml-5 text-sm">
            {!current.mutable && <p>Reopen this task before changing its requirement.</p>}
            <details onToggle={event => { const isOpen = event.currentTarget.open; setHistoryOpen(value => ({ ...value, [card.id]: isOpen })); }}>
              <summary aria-label={`Decision history for ${card.title}`} className="cursor-pointer">Details · Decision history</summary>
              {historyOpen[card.id] && <TaskRequirementHistory key={`${boardId}:${specId}:${card.id}`} boardId={boardId} specId={specId} cardId={card.id} />}
            </details>
          </div>}
          </div>;
        })}
      </fieldset>
      {loading && !basisChanged && <p role="status" className="text-sm">Loading task requirements…</p>}
      {basisChanged && <p role="alert" className="text-sm">The task or Spec changed. Review the latest requirements before saving. Your note and choice are preserved.</p>}
      {options && <>
        {!options.allowed_decisions.length && <p className="text-sm">Only an authorized human can change these requirements.</p>}
        {options.effects.dependent_tasks.length > 0 && <p className="text-sm">Dependencies stay unchanged. Dependent tasks: {options.effects.dependent_tasks.map(card => card.title).join(', ')}.</p>}
        {options.effects.test_scenarios_remain_governed && <p className="text-sm">This changes only the selected test tasks. Required scenarios for other tasks and the Spec still need verification.</p>}
      </>}
      {(canEdit && Boolean(options?.allowed_decisions.length) || uncertain) && <><label className="block text-sm">Requirement
        <select className="mt-1 w-full rounded border bg-white p-2 dark:bg-gray-900" value={choice} disabled={locked || !active || !options?.can_apply || loading || stale || basisChanged}
          onChange={event => setChoice(event.target.value as TaskRequirementChoice | '')}>
          <option value="">Select a requirement…</option>
          {(['required', 'optional', 'waived'] as const).filter(value => options?.allowed_decisions.includes(value) || value === choice)
            .map(value => <option key={value} value={value}>{requirementLabels[value]}</option>)}
        </select>
      </label>
      <label className="block text-sm">Note (optional)
        <textarea className="mt-1 w-full rounded border bg-white p-2 dark:bg-gray-900" value={note} maxLength={20000} disabled={locked || !active}
          onChange={event => setNote(event.target.value)} />
      </label></>}
      {error && <p role="alert" className="text-sm text-amber-700 dark:text-amber-300">{error}</p>}
      {!uncertain && (error || stale || basisChanged) && <button type="button" className="btn btn-secondary" disabled={busy || (loading && !basisChanged) || !selected.length || !active}
        onClick={() => { setRequestedRevision(revision); setRefresh(value => value + 1); }}>Review latest task requirements</button>}
      {canEdit && <button type="submit" className="btn btn-primary disabled:cursor-not-allowed disabled:opacity-50" disabled={!active || busy || (!uncertain && (loading || stale || basisChanged || !options?.can_apply || !choice || !options.allowed_decisions.includes(choice)))}>
        {busy ? 'Saving…' : uncertain ? 'Retry same decision' : 'Save task requirement'}
      </button>}
    </form>}
  </section>;
}
