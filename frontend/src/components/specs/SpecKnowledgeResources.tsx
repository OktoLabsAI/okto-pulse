import { useEffect, useRef, useState } from 'react';
import { Plus } from 'lucide-react';
import { KnowledgeWorkspace } from '@/components/resources/KnowledgeWorkspace';
import { useDashboardApi } from '@/services/api';
import type { SpecKnowledgeSummary } from '@/types';

interface Props {
  specId: string;
  boardId: string;
  revision: string | number;
  active: boolean;
  readCurrent: boolean;
  onReload: () => Promise<void>;
}

/** Native append/delete operations; no new approval or inferred replay receipt. */
export function SpecKnowledgeResources({ specId, boardId, revision, active, readCurrent, onReload }: Props) {
  const api = useDashboardApi();
  const [refresh, setRefresh] = useState(0);
  const [adding, setAdding] = useState(false);
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [content, setContent] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [denied, setDenied] = useState(false);
  const [confirmed, setConfirmed] = useState<{ kind: 'create' | 'delete'; id: string } | null>(null);
  const [uncertain, setUncertain] = useState(false);
  const [inspected, setInspected] = useState<SpecKnowledgeSummary[] | null>(null);
  const scope = `${boardId}:${specId}`;
  const context = useRef({ scope, generation: 0 });
  if (context.current.scope !== scope) context.current = { scope, generation: context.current.generation + 1 };
  const busy = useRef(false);
  const clearForm = () => { setAdding(false); setTitle(''); setDescription(''); setContent(''); };
  useEffect(() => {
    clearForm(); setPending(false); setError(null); setDenied(false); setConfirmed(null); setUncertain(false); setInspected(null);
    busy.current = false;
    return () => { context.current.generation += 1; };
  }, [scope]);
  const blocked = !active || !readCurrent || pending || denied || Boolean(confirmed) || uncertain;

  const handleError = (cause: unknown, writing: boolean) => {
    const status = typeof cause === 'object' && cause !== null && 'status' in cause ? Number(cause.status) : 0;
    if ([401, 403, 404].includes(status)) {
      context.current.generation += 1;
      clearForm(); setDenied(true); setConfirmed(null); setUncertain(false); setInspected(null);
      setPending(false); busy.current = false;
    } else if (writing && (!status || status >= 500)) { setUncertain(true); setInspected(null); }
    setError(cause instanceof Error ? cause.message : 'Knowledge operation could not be completed.');
  };

  const recover = async (receipt = confirmed) => {
    const origin = { ...context.current };
    const current = () => origin.scope === context.current.scope && origin.generation === context.current.generation;
    setPending(true); busy.current = true;
    try {
      // Inspect authoritative IDs, not title/content resemblance. Also confirm
      // the complete parent read before releasing a confirmed operation.
      const items = await api.listSpecKnowledge(specId);
      if (!current()) return false;
      if (receipt?.kind === 'create' && !items.some(item => item.id === receipt.id)) throw new Error('The saved knowledge item is not available in the current read yet.');
      if (receipt?.kind === 'delete' && items.some(item => item.id === receipt.id)) throw new Error('The deleted knowledge item is still present in this read.');
      await onReload();
      if (!current()) return false;
      setRefresh(value => value + 1); setError(null);
      if (receipt) { setConfirmed(null); clearForm(); }
      else if (uncertain) setInspected(items);
      return true;
    } catch (cause) {
      if (current()) handleError(cause, false);
      return false;
    } finally { if (current()) { setPending(false); busy.current = false; } }
  };

  const add = async () => {
    if (blocked || busy.current || !title.trim() || !content.trim()) return;
    const origin = { ...context.current };
    const current = () => origin.scope === context.current.scope && origin.generation === context.current.generation;
    setPending(true); busy.current = true; setError(null);
    try {
      const created = await api.createSpecKnowledge(specId, { title: title.trim(), description: description.trim() || undefined, content: content.trim() });
      if (!current()) return;
      const receipt = { kind: 'create' as const, id: created.id };
      setConfirmed(receipt);
      await recover(receipt);
    } catch (cause) { if (current()) handleError(cause, true); }
    finally { if (current()) { setPending(false); busy.current = false; } }
  };

  const remove = async (id: string) => {
    if (blocked || busy.current || adding || !window.confirm('Delete this knowledge base item?')) return false;
    const origin = { ...context.current };
    const current = () => origin.scope === context.current.scope && origin.generation === context.current.generation;
    setPending(true); busy.current = true; setError(null);
    try {
      await api.deleteSpecKnowledge(specId, id);
      if (!current()) return false;
      const receipt = { kind: 'delete' as const, id };
      setConfirmed(receipt);
      return await recover(receipt);
    } catch (cause) { if (current()) handleError(cause, true); return false; }
    finally { if (current()) { setPending(false); busy.current = false; } }
  };

  if (denied) return <div role="alert">Knowledge access is no longer available.</div>;
  return <div className="space-y-3">
    {!readCurrent && <p role="status" className="text-sm text-amber-700 dark:text-amber-300">Current knowledge could not be verified. Your input is preserved; reload before another change.</p>}
    {error && <p role="alert" className="text-sm text-red-600 dark:text-red-400">{error}</p>}
    {confirmed && <p role="status" className="text-sm text-amber-700 dark:text-amber-300">Knowledge {confirmed.kind === 'delete' ? 'deletion' : 'creation'} was confirmed. Reload before another change.</p>}
    {uncertain && <div role="alert" className="space-y-2 rounded-lg border border-amber-300 p-3 text-sm dark:border-amber-800">
      <p>The response was not confirmed. The write will not be repeated automatically. Inspect current knowledge before discarding this attempt.</p>
      {inspected && <ul>{inspected.map(item => <li key={item.id}>{item.title} · {item.id}</li>)}</ul>}
      <button type="button" className="btn btn-secondary text-xs" disabled={!inspected || pending || !readCurrent} onClick={() => {
        setUncertain(false); setInspected(null); setError(null); clearForm();
      }}>I reviewed knowledge; discard local attempt</button>
    </div>}
    {(confirmed || uncertain || !readCurrent) && <button type="button" className="btn btn-secondary text-xs" disabled={!active || pending} onClick={() => { void recover(); }}>
      {confirmed ? 'Reload saved knowledge' : 'Reload current knowledge'}
    </button>}
    <KnowledgeWorkspace boardId={boardId} entityType="spec" entityId={specId} active={active}
      refreshKey={`${revision}:${refresh}`} onDelete={remove} canDelete={() => !blocked && !adding} />
    {adding ? <fieldset disabled={pending || Boolean(confirmed) || uncertain} className="min-w-0 rounded-lg border border-amber-200 bg-amber-50/50 p-3 space-y-2 dark:border-amber-700 dark:bg-amber-900/10">
      <input aria-label="Knowledge title" value={title} onChange={event => setTitle(event.target.value)} placeholder="Title" className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-700" />
      <input aria-label="Knowledge description" value={description} onChange={event => setDescription(event.target.value)} placeholder="Description (optional)" className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-700" />
      <textarea aria-label="Knowledge content" value={content} onChange={event => setContent(event.target.value)} placeholder="Content (markdown, text, JSON...)" rows={6} className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-700" />
      <div className="flex justify-end gap-2">
        <button type="button" onClick={clearForm} className="btn btn-secondary text-xs">Cancel</button>
        <button type="button" onClick={() => { void add(); }} disabled={blocked || !title.trim() || !content.trim()} className="btn btn-primary text-xs">Add</button>
      </div>
    </fieldset> : <button type="button" disabled={blocked} onClick={() => setAdding(true)} className="flex items-center gap-1 text-sm text-amber-600 hover:text-amber-800 dark:text-amber-400 dark:hover:text-amber-300">
      <Plus size={14} /> Add Knowledge
    </button>}
  </div>;
}
