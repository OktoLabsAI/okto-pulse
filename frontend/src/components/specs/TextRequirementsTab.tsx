import { useRef, useState } from 'react';
import { CheckCircle, ChevronDown, ChevronUp, FileText, Pencil, Plus, Trash2, XCircle } from 'lucide-react';
import type { SpecTextRequirement } from '@/types';

export interface TextRequirementDraft { title: string; text: string }
interface Props {
  kind: 'FR' | 'AC';
  items: SpecTextRequirement[];
  canCreate: boolean; canEdit: boolean; canRevoke: boolean;
  onSave: (id: string | null, draft: TextRequirementDraft) => Promise<void>;
  onRevoke: (id: string) => Promise<void>;
}
const button = 'inline-flex min-h-9 items-center justify-center gap-1.5 rounded-lg border border-gray-300 px-3 text-xs font-medium hover:bg-gray-100 disabled:opacity-50 dark:border-gray-600 dark:hover:bg-gray-700';
const input = 'mt-1 block w-full rounded-lg border border-gray-300 bg-white p-2 text-sm dark:border-gray-600 dark:bg-gray-900';

/** Edits stable entities directly; text equality never determines identity. */
export function TextRequirementsTab({ kind, items, canCreate, canEdit, canRevoke, onSave, onRevoke }: Props) {
  const [editor, setEditor] = useState<{ id: string | null; draft: TextRequirementDraft } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const writing = useRef(false);
  const rows = items.filter(item => (item.status || 'active') === 'active');
  const title = kind === 'FR' ? 'Functional requirements' : 'Acceptance criteria';
  async function write(action: () => Promise<void>) {
    if (writing.current) return;
    writing.current = true; setBusy(true); setError('');
    try { await action(); setEditor(null); }
    catch (cause) { setError(cause instanceof Error ? cause.message : 'Could not save. Refresh the Spec before retrying.'); }
    finally { writing.current = false; setBusy(false); }
  }
  const form = editor && (editor.id ? canEdit : canCreate) && <form onSubmit={event => {
    event.preventDefault();
    if (editor.draft.title.trim() && editor.draft.text.trim()) void write(() => onSave(editor.id, { title: editor.draft.title.trim(), text: editor.draft.text.trim() }));
  }} className="border border-sky-200 dark:border-sky-700 rounded-lg p-3 space-y-2 bg-sky-50/50 dark:bg-sky-900/10">
    <fieldset disabled={busy} className="space-y-3">
      <legend className="mb-2 text-sm font-semibold">{editor.id ? 'Edit' : 'New'} {kind}</legend>
      <label className="block text-sm font-medium">Title<input autoFocus required className={input} value={editor.draft.title} onChange={event => setEditor({ ...editor, draft: { ...editor.draft, title: event.target.value } })} /></label>
      <label className="block text-sm font-medium">Content<textarea required rows={5} className={input} value={editor.draft.text} onChange={event => setEditor({ ...editor, draft: { ...editor.draft, text: event.target.value } })} /></label>
      <div className="flex gap-2"><button type="submit" className={`${button} bg-blue-600 text-white hover:bg-blue-700`} disabled={!editor.draft.title.trim() || !editor.draft.text.trim()}>{busy ? 'Saving…' : 'Save'}</button><button type="button" className={button} onClick={() => setEditor(null)}>Cancel</button></div>
    </fieldset>
  </form>;
    const linked = rows.filter(item => (item.linked_task_ids?.length || 0) > 0).length;
    const percent = rows.length ? Math.round(linked / rows.length * 100) : 0;
    return <section aria-label={title} className="space-y-4">
      <h3 className="sr-only">{title}</h3>
      {kind === 'FR' && rows.length > 0 && <div className="border border-gray-200 dark:border-gray-700 rounded-lg p-3">
        <div className="flex items-center justify-between mb-2">
          <h4 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide">FR Task Coverage ({linked}/{rows.length})</h4>
          <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${linked === rows.length ? 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300' : 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300'}`}>{percent}% linked</span>
        </div>
        <div role="progressbar" aria-label="FR task links" aria-valuenow={percent} aria-valuemin={0} aria-valuemax={100} className="h-2 bg-gray-100 dark:bg-gray-700 rounded-full overflow-hidden">
          <div className={`h-full transition-all duration-500 rounded-full ${linked === rows.length ? 'bg-green-500' : 'bg-amber-500'}`} style={{ width: `${percent}%` }} />
        </div>
      </div>}
      {error && <p role="alert" className="rounded-lg border border-red-300 p-3 text-sm text-red-600 dark:text-red-400">{error}</p>}
      {!rows.length && editor?.id !== null && <div className="text-center py-6"><FileText size={32} className="mx-auto text-gray-300 dark:text-gray-600 mb-2" /><p className="text-sm text-gray-500 dark:text-gray-400">No {title.toLowerCase()} defined</p></div>}
      {rows.map((item, index) => {
        if (editor?.id === item.id && canEdit) return <div key={item.id}>{form}</div>;
        const expanded = expandedId === item.id;
        const taskCount = item.linked_task_ids?.length || 0;
        const label = item.title || (kind === 'AC' ? item.text : null) || `${kind} ${index + 1} — Title not defined`;
        return <article key={item.id} aria-label={label} className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
          <header className="flex items-center gap-2 px-3 py-2 bg-gray-50 dark:bg-gray-700/50">
            <button type="button" aria-expanded={expanded} aria-controls={`requirement-details-${item.id}`} onClick={() => setExpandedId(expanded ? null : item.id)} className="flex min-w-0 flex-1 items-center gap-2 text-left">
              {kind === 'FR' && (taskCount > 0 ? <CheckCircle size={14} className="text-green-500 shrink-0" /> : <XCircle size={14} className="text-gray-300 dark:text-gray-600 shrink-0" />)}
              <span title={item.id} className="max-w-[40%] truncate shrink-0 text-[10px] px-1.5 py-0.5 rounded bg-sky-100 text-sky-700 dark:bg-sky-900/40 dark:text-sky-300 font-medium">{kind === 'FR' ? item.id : 'AC'}</span>
              <span className="text-sm font-medium text-gray-900 dark:text-white truncate flex-1" title={label}>{label}</span>
              {kind === 'FR' && <span className={`shrink-0 text-[10px] px-1.5 py-0.5 rounded ${taskCount > 0 ? 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300' : 'bg-gray-100 text-gray-400 dark:bg-gray-700 dark:text-gray-500'}`}>{taskCount} tasks</span>}
              {expanded ? <ChevronUp size={14} className="text-gray-400 shrink-0" /> : <ChevronDown size={14} className="text-gray-400 shrink-0" />}
            </button>
            {canEdit && <button type="button" aria-label={`Edit ${item.id}`} title="Edit" disabled={busy} className="p-0.5 text-gray-400 hover:text-blue-500 disabled:opacity-50" onClick={() => { setError(''); setEditor({ id: item.id, draft: { title: item.title || '', text: item.text } }); }}><Pencil size={12} /></button>}
            {canRevoke && <button type="button" aria-label={`Revoke ${item.id}`} title="Revoke" disabled={busy} className="p-0.5 text-gray-400 hover:text-red-500 disabled:opacity-50" onClick={() => void write(() => onRevoke(item.id))}><Trash2 size={12} /></button>}
          </header>
          {expanded && <div id={`requirement-details-${item.id}`} className="px-3 py-2 space-y-2 text-xs text-gray-600 dark:text-gray-400">
            <p className="text-xs text-gray-600 dark:text-gray-400 whitespace-pre-wrap break-words">{item.text}</p>
            {taskCount > 0 && <div className="flex flex-wrap gap-1"><span className="text-[10px] text-gray-400 mr-1">Linked Tasks:</span>{item.linked_task_ids!.map(id => <span key={id} title={id} className="text-[10px] px-1.5 py-0.5 rounded bg-green-50 text-green-700 dark:bg-green-900/20 dark:text-green-300">{id.slice(0, 8)}</span>)}</div>}
            <details className="text-xs text-gray-500 dark:text-gray-400"><summary className="cursor-pointer">Reference</summary><code className="mt-1 block break-all">{item.id}</code></details>
          </div>}
        </article>;
      })}
      {editor?.id === null ? form : canCreate && <button type="button" disabled={busy} onClick={() => { setError(''); setEditor({ id: null, draft: { title: '', text: '' } }); }} className="w-full py-2 border-2 border-dashed border-gray-300 dark:border-gray-600 rounded-lg text-sm text-gray-500 hover:border-sky-400 hover:text-sky-500 transition-colors flex items-center justify-center gap-1 disabled:opacity-50"><Plus size={14} />{kind === 'FR' ? 'Add Functional Requirement' : 'Add AC'}</button>}
    </section>;

}
