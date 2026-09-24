import { useEffect, useRef, useState } from 'react';
import { v4 as uuid } from 'uuid';
import { usePermissions } from '@/hooks/usePermissions';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import { useLearningCaptureApi, type CaptureHistory, type CaptureRequest, type CaptureSource } from '@/services/learning-capture-api';

const CAPTURE_SOURCE_PERMISSIONS = ['board.read', 'card.entity.read', 'card.entity.context_read',
  'card.validation.read', 'card.comments.read', 'card.conclusion.read', 'card.tests.read', 'spec.entity.read', 'spec.tests.read'];
const CREATE = ['kg.session.begin', 'kg.session.add_node', 'kg.session.add_edge', 'kg.session.commit'];
const fieldClass = 'w-full rounded border border-gray-300 bg-transparent p-2 dark:border-gray-600';
const buttonClass = 'rounded border px-3 py-1.5 text-sm disabled:opacity-50';

function failure(error: unknown) {
  if (error instanceof AuthenticatedFetchError) {
    if ([401, 403, 404].includes(error.status)) return 'Learning access is unavailable. Check your permissions and refresh.';
    if (error.status === 409) return 'The evidence or capture changed. Refresh the evidence and review applicability before saving again.';
    if (error.status === 422) return 'The capture could not be accepted. Check the text and selected evidence.';
  }
  return 'Learning information could not be verified. Your text is preserved; retry when available.';
}

export function LearningCapturePanel({ boardId, bugId }: { boardId: string; bugId: string }) {
  const permissions = usePermissions(boardId);
  if (permissions.isLoading) return <p role="status">Loading Learning permissions…</p>;
  if (permissions.error) return <p role="alert">Learning permissions could not be verified.</p>;
  if (!CAPTURE_SOURCE_PERMISSIONS.every(permissions.has)) return null;
  return <CaptureEditor key={`${boardId}:${bugId}`} boardId={boardId} bugId={bugId}
    canCreate={CREATE.every(permissions.has)} canReadHistory={permissions.has('kg.query.learning_from_bugs')} />;
}

function CaptureEditor({ boardId, bugId, canCreate, canReadHistory }: {
  boardId: string; bugId: string; canCreate: boolean; canReadHistory: boolean;
}) {
  const api = useLearningCaptureApi();
  const [source, setSource] = useState<CaptureSource | null>(null);
  const [sourceError, setSourceError] = useState('');
  const [refresh, setRefresh] = useState(0);
  const [historyRefresh, setHistoryRefresh] = useState(0);
  const [cursor, setCursor] = useState<string | null>(null);
  const [history, setHistory] = useState<CaptureHistory | null>(null);
  const [historyError, setHistoryError] = useState('');
  const [draft, setDraft] = useState({ content: '', context: '', applicability: '' });
  const [selected, setSelected] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [saveError, setSaveError] = useState('');
  const [mustRefresh, setMustRefresh] = useState(false);
  const attempt = useRef<{ basis: string; id: string } | null>(null);
  const submission = useRef<AbortController | null>(null);
  useEffect(() => () => { submission.current?.abort(); }, []);

  useEffect(() => {
    const abort = new AbortController();
    setSource(null); setSourceError('');
    api.source(boardId, bugId, abort.signal).then(value => {
      if (abort.signal.aborted) return;
      setSource(value); setMustRefresh(false);
      setSelected(previous => previous.filter(id => value.scenarios.some(row => row.id === id && row.authenticated)));
    }).catch(error => { if (!abort.signal.aborted) setSourceError(failure(error)); });
    return () => abort.abort();
  }, [api, boardId, bugId, refresh]);

  useEffect(() => {
    const abort = new AbortController();
    setHistory(null); setHistoryError('');
    if (canReadHistory) api.history(boardId, bugId, cursor, abort.signal).then(value => {
      if (!abort.signal.aborted) setHistory(value);
    }).catch(error => { if (!abort.signal.aborted) setHistoryError(failure(error)); });
    return () => abort.abort();
  }, [api, boardId, bugId, canReadHistory, cursor, historyRefresh]);

  async function save() {
    if (!source || !canCreate || saving || saved || mustRefresh || !selected.length) return;
    const base = { board_id: boardId, expected_source_digest: source.source_digest,
      expected_source_version: source.source_policy_version, ...draft, scenario_ids: [...selected].sort() };
    const basis = JSON.stringify(base);
    if (attempt.current?.basis !== basis) attempt.current = { basis, id: uuid() };
    const request: CaptureRequest = { ...base, capture_id: attempt.current.id };
    const abort = new AbortController(); submission.current = abort;
    setSaving(true); setSaveError('');
    try {
      await api.create(bugId, request, abort.signal);
      if (abort.signal.aborted) return;
      setSaved(true); setCursor(null); setHistoryRefresh(value => value + 1);
    } catch (error) {
      if (abort.signal.aborted) return;
      setSaveError(failure(error));
      if (error instanceof AuthenticatedFetchError && error.status === 409) setMustRefresh(true);
    } finally { if (!abort.signal.aborted) setSaving(false); }
  }

  return <section aria-label="Bug Learnings" className="mt-4 space-y-4 rounded border border-gray-200 p-4 dark:border-gray-700">
    <h3 className="font-semibold">Learnings</h3>
    <p className="text-sm text-gray-500">Record what this correction teaches and where it applies. Saving a Learning does not approve the implementation or close the Bug.</p>
    {canCreate ? <div className="space-y-3">
      {(['content', 'context', 'applicability'] as const).map(name => <label key={name} className="block text-sm">
        {{ content: 'Learning', context: 'Context', applicability: 'Applicability' }[name]}
        <textarea className={fieldClass} value={draft[name]} maxLength={65536} disabled={saving || saved}
          onChange={event => setDraft(previous => ({ ...previous, [name]: event.target.value }))} />
      </label>)}
      {sourceError ? <p role="alert">{sourceError}</p> : !source ? <p role="status">Loading current evidence…</p> :
        <fieldset disabled={saving || saved} className="space-y-2">
          <legend className="text-sm font-medium">Evidence supporting this Learning</legend>
          {source.scenarios.filter(row => row.authenticated).length === 0 && <p>No authenticated scenario evidence is available.</p>}
          {source.scenarios.map(row => <label key={row.id} className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={selected.includes(row.id)} disabled={!row.authenticated}
              onChange={event => setSelected(previous => event.target.checked ? [...previous, row.id] : previous.filter(id => id !== row.id))} />
            {row.title}{!row.authenticated && ' — authenticated evidence unavailable'}
          </label>)}
        </fieldset>}
      {saveError && <p role="alert">{saveError}</p>}
      {saved && <p role="status">Learning saved. Graph materialization is pending.</p>}
      <div className="flex flex-wrap gap-2">
        <button type="button" className={buttonClass} disabled={saving} onClick={() => setRefresh(value => value + 1)}>Refresh evidence</button>
        <button type="button" className={buttonClass} onClick={() => void save()}
          disabled={saving || saved || !source || mustRefresh || !selected.length || selected.length > 128 || !Object.values(draft).every(value => value.trim())}>
          {saving ? 'Saving Learning…' : 'Save Learning'}
        </button>
        {saved && <button type="button" className={buttonClass} onClick={() => {
          setSaved(false); setDraft({ content: '', context: '', applicability: '' }); setSelected([]);
          attempt.current = null; setRefresh(value => value + 1);
        }}>Write another Learning</button>}
      </div>
    </div> : <p className="text-sm">You do not have permission to create a Learning.</p>}
    {canReadHistory && <div className="space-y-3" aria-label="Saved Learnings">
      <h4 className="font-medium">Saved Learnings</h4>
      <p className="text-sm text-gray-500">Historical captures do not establish applicability to the current correction.</p>
      <button type="button" className={buttonClass} onClick={() => { setHistory(null); setCursor(null); setHistoryRefresh(value => value + 1); }}>Refresh saved Learnings</button>
      {historyError ? <p role="alert">{historyError}</p> : !history ? <p role="status">Loading saved Learnings…</p> : <>
        {history.items.length === 0 && <p>No saved Learnings on this page.</p>}
        {history.items.map(item => <article className="space-y-1 rounded border p-3" key={`${item.learning_id}:${item.generation}:${item.source_revision}`}>
          <p className="text-xs text-gray-500">{item.capture.author_id} · {new Date(item.capture.captured_at).toLocaleString()}</p>
          <p className="whitespace-pre-wrap">{item.capture.content}</p>
          <p className="whitespace-pre-wrap text-sm"><strong>Context: </strong>{item.capture.context}</p>
          <p className="whitespace-pre-wrap text-sm"><strong>Applicability: </strong>{item.capture.applicability}</p>
          {source && item.capture.source.digest !== source.source_digest && <p className="text-sm text-amber-700 dark:text-amber-300">Recorded against an earlier evidence basis. Review applicability before reuse.</p>}
        </article>)}
        {history.next_cursor && <button type="button" className={buttonClass} onClick={() => { setHistory(null); setCursor(history.next_cursor); }}>Next Learning page</button>}
      </>}
    </div>}
  </section>;
}
