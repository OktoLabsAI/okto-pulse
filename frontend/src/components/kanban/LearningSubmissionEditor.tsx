import { useEffect, useState } from 'react';
import { useLearningCaptureApi, type CaptureSource } from '@/services/learning-capture-api';
import type { LearningSubmission } from '@/types';

type Draft = Omit<LearningSubmission, 'capture_id'>;
type Change = (draft: Draft | null, pending: boolean) => void;

export function LearningSubmissionEditor({ boardId, bugId, canCreate, onChange }: {
  boardId: string; bugId: string; canCreate: boolean; onChange: Change;
}) {
  const [enabled, setEnabled] = useState(false);
  useEffect(() => {
    if (!enabled) onChange(null, false);
    else if (!canCreate) onChange(null, true);
  }, [enabled, canCreate, onChange]);
  return <section aria-label="Learning with this report" className="mt-3 space-y-2 rounded border p-3">
    <label><input type="checkbox" checked={enabled} disabled={!enabled && !canCreate}
      onChange={event => { setEnabled(event.target.checked); onChange(null, event.target.checked); }} /> Record a Learning with this report</label>
    {enabled && (canCreate ? <SubmissionFields key={`${boardId}:${bugId}`} boardId={boardId} bugId={bugId} onChange={onChange} />
      : <p role="alert">Learning authoring permission is unavailable. Restore access or remove the Learning from this submission.</p>)}
  </section>;
}

function SubmissionFields({ boardId, bugId, onChange }: { boardId: string; bugId: string; onChange: Change }) {
  const api = useLearningCaptureApi();
  const [source, setSource] = useState<CaptureSource | null>(null);
  const [error, setError] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [text, setText] = useState({ content: '', context: '', applicability: '' });
  const [selected, setSelected] = useState<string[]>([]);
  useEffect(() => {
    const abort = new AbortController();
    setSource(null); setError(false); setSelected([]);
    api.source(boardId, bugId, abort.signal).then(value => {
      if (!abort.signal.aborted) setSource(value);
    }).catch(() => { if (!abort.signal.aborted) setError(true); });
    return () => abort.abort();
  }, [api, boardId, bugId, refresh]);
  useEffect(() => {
    const valid = source && selected.length > 0 && Object.values(text).every(value => value.trim().length > 0);
    onChange(valid ? { ...text, expected_source_digest: source.source_digest,
      expected_source_version: source.source_policy_version, scenario_ids: [...selected].sort() } : null, !valid);
  }, [source, selected, text, onChange]);
  return <div className="space-y-2">
    <p className="text-sm">Save the lesson and this execution report together. The Learning does not approve the implementation.</p>
    {(['content', 'context', 'applicability'] as const).map(name => <label key={name} className="block text-sm">
      {{ content: 'Learning for this report', context: 'Learning context', applicability: 'Learning applicability' }[name]}
      <textarea maxLength={65536} value={text[name]} className="block w-full rounded border bg-transparent p-2"
        onChange={event => setText(previous => ({ ...previous, [name]: event.target.value }))} />
    </label>)}
    {error ? <p role="alert">Learning evidence is unavailable. Your text is preserved; refresh to try again.</p>
      : !source ? <p role="status">Loading Learning evidence…</p>
        : <fieldset><legend>Evidence supporting this lesson</legend>
          {source.scenarios.filter(row => row.authenticated).length === 0 && <p>No authenticated scenario evidence is available.</p>}
          {source.scenarios.map(row => <label key={row.id} className="block text-sm">
            <input type="checkbox" checked={selected.includes(row.id)} disabled={!row.authenticated}
              onChange={event => setSelected(previous => event.target.checked ? [...previous, row.id] : previous.filter(id => id !== row.id))} />
            {row.title}{!row.authenticated && ' — authenticated evidence unavailable'}
          </label>)}
        </fieldset>}
    <button type="button" onClick={() => { setSource(null); setSelected([]); onChange(null, true); setRefresh(value => value + 1); }}>Refresh Learning evidence</button>
  </div>;
}
