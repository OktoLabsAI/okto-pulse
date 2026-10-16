import { useEffect, useRef, useState } from 'react';
import { useLearningCaptureApi, type LearningCandidate, type LearningCandidates } from '@/services/learning-capture-api';
import type { LearningIntentRequest } from '@/types';

export interface LearningIntentSelection { intent?: LearningIntentRequest; ready: boolean; content?: string }
const buttonClass = 'rounded border px-3 py-1 text-sm disabled:opacity-50';

export function LearningIntentSelector({ boardId, bugId, content, refresh, disabled = false, onChange }: {
  boardId: string; bugId: string; content: string; refresh: number; disabled?: boolean;
  onChange: (value: LearningIntentSelection) => void;
}) {
  const api = useLearningCaptureApi();
  const [query, setQuery] = useState<string | null>(null);
  const [results, setResults] = useState<LearningCandidates | null>(null);
  const [error, setError] = useState(false);
  const [loading, setLoading] = useState(false);
  const [mode, setMode] = useState<'create' | 'reuse' | 'supersede'>('create');
  const [target, setTarget] = useState<LearningCandidate | null>(null);
  const [reason, setReason] = useState('');
  const search = useRef<AbortController | null>(null);
  const lastRefresh = useRef(refresh);
  useEffect(() => () => { search.current?.abort(); }, []);
  useEffect(() => {
    if (lastRefresh.current === refresh) return;
    lastRefresh.current = refresh;
    search.current?.abort(); setLoading(false); setResults(null); setError(false); setTarget(null);
    if (mode !== 'create') onChange({ ready: false });
  }, [refresh, mode, onChange]);

  function choose(next: typeof mode, candidate: LearningCandidate | null, explanation = reason) {
    setMode(next); setTarget(candidate); setReason(explanation);
    if (next === 'create') { onChange({ ready: true }); return; }
    if (!candidate) { onChange({ ready: false }); return; }
    const reference = { target_node_id: candidate.learning_id, target_generation: candidate.generation,
      expected_fingerprint: candidate.fingerprint, reason: explanation };
    const intent: LearningIntentRequest = next === 'reuse' ? { ...reference, kind: 'reuse' }
      : { ...reference, kind: 'supersede', scope: 'source_bug' };
    onChange({ intent, ready: explanation.trim().length > 0,
      ...(next === 'reuse' ? { content: candidate.content } : {}) });
  }
  async function find() {
    const text = query ?? content;
    if (disabled || !text.trim() || text.length > 4096) return;
    search.current?.abort();
    const abort = new AbortController(); search.current = abort;
    setResults(null); setError(false); setLoading(true); setTarget(null);
    if (mode !== 'create') onChange({ ready: false });
    try {
      const page = await api.candidates(boardId, bugId, text, abort.signal);
      if (!abort.signal.aborted) setResults(page);
    } catch { if (!abort.signal.aborted) setError(true); }
    finally { if (!abort.signal.aborted) setLoading(false); }
  }
  return <fieldset disabled={disabled} className="space-y-2 rounded border p-3" aria-label="Learning relationship">
    <legend>Learning relationship</legend>
    <button type="button" className={buttonClass} aria-pressed={mode === 'create'} onClick={() => choose('create', null, '')}>Create a new Learning</button>
    <p className="text-sm">Finding related Learnings is optional. Similarity does not establish applicability or approval.</p>
    <label className="block text-sm">Find related Learnings
      <textarea value={query ?? content} maxLength={4096} className="block w-full rounded border bg-transparent p-2"
        onChange={event => setQuery(event.target.value)} />
    </label>
    {(query ?? content).length > 4096 && <p>Shorten the search text to 4096 characters.</p>}
    <button type="button" className={buttonClass} disabled={loading || !(query ?? content).trim() || (query ?? content).length > 4096}
      onClick={() => void find()}>Find suggestions</button>
    {loading && <p role="status">Finding related Learnings…</p>}
    {(error || results?.status === 'unavailable') && <p role="alert">Suggestions are unavailable. You can still create a new Learning.</p>}
    {results?.status === 'available' && <div className="space-y-2">
      <p className="text-sm">Up to three suggestions; this is not an exhaustive search. Review each Learning's context.</p>
      {results.limitations.length > 0 && <p>Some candidates could not be verified and were omitted.</p>}
      {results.items.length === 0 && <p>No verified suggestions in this search window.</p>}
      {results.items.map(candidate => <article className="space-y-1 rounded border p-2" key={`${candidate.learning_id}:${candidate.generation}`}>
        <p className="whitespace-pre-wrap">{candidate.content}</p>
        <p className="whitespace-pre-wrap text-sm">{candidate.context}</p>
        <p className="text-sm">Similarity: {candidate.similarity.toFixed(2)} · {candidate.suggestion === 'reuse' ? 'Consider reuse'
          : candidate.suggestion === 'review_replacement' ? 'Review for a possible replacement' : 'Related Learning'}</p>
        <div className="flex gap-2">
          <button type="button" className={buttonClass} onClick={() => choose('reuse', candidate, '')}>Reuse this Learning</button>
          <button type="button" className={buttonClass} onClick={() => choose('supersede', candidate, '')}>Replace for this Bug</button>
        </div>
      </article>)}
    </div>}
    {mode !== 'create' && <div className="space-y-2">
      {!target ? <p role="alert">Choose a target again, or explicitly create a new Learning.</p> : <>
        <p>{mode === 'reuse' ? 'Reusing the selected Learning; its content is preserved.' : 'Replacing the selected Learning for this Bug only. Other origins remain unchanged.'}</p>
        <p>Selected Learning: <code>{target.learning_id}</code></p>
        <label className="block text-sm">Reason for {mode === 'reuse' ? 'reuse' : 'replacement'}
          <textarea value={reason} maxLength={16384} className="block w-full rounded border bg-transparent p-2"
            onChange={event => choose(mode, target, event.target.value)} />
        </label>
      </>}
    </div>}
  </fieldset>;
}
