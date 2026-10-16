import { useEffect, useState } from 'react';
import { useLearningCaptureApi, type CaptureHistory, type CaptureSource } from '@/services/learning-capture-api';
import type { LearningCaptureSelection } from '@/types';

export function LearningCaptureSelector({ boardId, bugId, disabled, onChange }: {
  boardId: string; bugId: string; disabled: boolean;
  onChange: (selection: LearningCaptureSelection | null) => void;
}) {
  const [open, setOpen] = useState(false);
  return <section aria-label="Learning for this validation" className="space-y-2 rounded border p-3">
    <p className="text-sm">Link a saved Learning to this Bug completion. A valid capture is required when the Board's Bug Learning policy is Blocking. The server revalidates its evidence; selecting it does not approve the Bug.</p>
    {!open ? <button type="button" disabled={disabled} onClick={() => setOpen(true)}>Choose saved Learning</button>
      : <CaptureChoices key={`${boardId}:${bugId}`} boardId={boardId} bugId={bugId} disabled={disabled} onChange={onChange} />}
  </section>;
}

function CaptureChoices({ boardId, bugId, disabled, onChange }: {
  boardId: string; bugId: string; disabled: boolean;
  onChange: (selection: LearningCaptureSelection | null) => void;
}) {
  const api = useLearningCaptureApi();
  const [source, setSource] = useState<CaptureSource | null>(null);
  const [history, setHistory] = useState<CaptureHistory | null>(null);
  const [error, setError] = useState(false);
  const [cursor, setCursor] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [selected, setSelected] = useState('');
  useEffect(() => {
    const abort = new AbortController();
    setSource(null); setHistory(null); setError(false); setSelected(''); onChange(null);
    Promise.all([api.source(boardId, bugId, abort.signal), api.history(boardId, bugId, cursor, abort.signal)])
      .then(([basis, page]) => { if (!abort.signal.aborted) { setSource(basis); setHistory(page); } })
      .catch(() => { if (!abort.signal.aborted) setError(true); });
    return () => abort.abort();
  }, [api, boardId, bugId, cursor, refresh, onChange]);
  function clear() { setSelected(''); onChange(null); }
  return <fieldset disabled={disabled} className="space-y-2">
    <legend>Saved Learnings — subject to revalidation</legend>
    {error ? <p role="alert">Learning evidence is unavailable. Refresh to try again.</p>
      : !source || !history ? <p role="status">Loading Learning evidence…</p>
        : history.items.length === 0 ? <p>No saved Learnings on this page.</p>
          : history.items.map(row => {
            const key = `${row.learning_id}:${row.generation}:${row.fingerprint}`;
            const matching = row.capture.source.digest === source.source_digest
              && row.capture.source.policy_version === source.source_policy_version;
            return <label key={key} className="block whitespace-pre-wrap text-sm">
              <input type="radio" name={`learning-${bugId}`} checked={selected === key} disabled={!matching}
                onChange={() => { setSelected(key); onChange({ learning_id: row.learning_id, generation: row.generation, fingerprint: row.fingerprint }); }} />
              {row.capture.content}
              <span className="block">Context: {row.capture.context}</span>
              <span className="block">Applicability: {row.capture.applicability}</span>
              {!matching && <span className="block">Captured against a different Bug version. Review and save a new Learning before selecting.</span>}
            </label>;
          })}
    <button type="button" onClick={clear}>Clear Learning selection</button>
    <button type="button" onClick={() => { clear(); setRefresh(value => value + 1); }}>Refresh Learnings</button>
    {history?.next_cursor && <button type="button" onClick={() => { clear(); setCursor(history.next_cursor); }}>Next Learnings</button>}
    {cursor && <button type="button" onClick={() => { clear(); setCursor(null); }}>First Learnings</button>}
  </fieldset>;
}
