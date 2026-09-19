import { useEffect, useRef, useState } from 'react';
import { useDashboardApi } from '@/services/api';
import type { CardDeliveryEvidenceInput, DeliveryPerCard } from '@/types/delivery-evidence';

export function CardProgressPanel({ boardId, specId, edition, card, canWrite, onSaved }: {
  boardId: string; specId: string; edition: number; card: DeliveryPerCard;
  canWrite: boolean; onSaved: () => void;
}) {
  const api = useDashboardApi();
  const [summary, setSummary] = useState('');
  const [remaining, setRemaining] = useState('');
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const pending = useRef(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const retry = useRef<{ body: string; key: string } | null>(null);
  const writable = canWrite && ['started', 'in_progress'].includes(card.status) && !!card.card_version;

  async function save() {
    if (!writable || pending.current || !summary.trim() || !remaining.trim()) return;
    const input: CardDeliveryEvidenceInput = {
      expected_card_version: card.card_version!, expected_spec_edition: edition,
      idempotency_key: '', kind: 'progress', obligation_refs: [], justification: summary.trim(),
      progress: { contract_version: 'delivery-progress/v1', remaining: remaining.trim(),
        source_state: { workspace_state: dirty ? 'dirty' : 'unknown', recoverability: dirty ? 'external_workspace' : 'unknown' } },
    };
    const body = JSON.stringify(input);
    if (retry.current?.body !== body) retry.current = { body, key: crypto.randomUUID() };
    input.idempotency_key = retry.current.key;
    pending.current = true; setBusy(true); setError('');
    try {
      await api.recordCardDeliveryEvidence(boardId, card.card_id, specId, input);
      if (!mounted.current) return;
      retry.current = null; setSummary(''); setRemaining(''); setDirty(false); onSaved();
    } catch (err) {
      if (mounted.current) setError(err instanceof Error ? err.message : 'Progress could not be saved. Retry with the same content.');
    } finally { pending.current = false; if (mounted.current) setBusy(false); }
  }

  return <section aria-label="Recorded progress" className="space-y-2 rounded border p-3 text-sm">
    <h3>Recorded progress</h3>
    <p>Declared work does not complete this card or satisfy delivery gates. Recovery is limited to confirmed records and material you can access.</p>
    {card.progress?.items.map(item => <article key={item.id} className="border-t py-2">
      <p>{item.summary}</p><p>Remaining: {item.remaining}</p>
      {item.revoked && <p>Revoked by an authorized reviewer; retained as history.</p>}
      <p className="text-xs">{item.actor_id} · {item.created_at} · {item.source_state.workspace_state} · {item.source_state.recoverability}</p>
      {item.text_truncated && <p>This record is shortened in the summary.</p>}
    </article>)}
    {card.progress?.truncated && <p>Showing recent records from {card.progress.total} saved checkpoints. This is an incomplete history.</p>}
    {error && <p role="alert">{error}</p>}
    {writable ? <div className="space-y-2">
      <label className="block">Work recorded<textarea aria-label="Work recorded" value={summary} maxLength={20000} onChange={e => setSummary(e.target.value)} disabled={busy} className="block w-full border bg-transparent" /></label>
      <label className="block">Remaining work<textarea aria-label="Remaining work" value={remaining} maxLength={8000} onChange={e => setRemaining(e.target.value)} disabled={busy} className="block w-full border bg-transparent" /></label>
      <label className="block"><input type="checkbox" checked={dirty} onChange={e => setDirty(e.target.checked)} disabled={busy} /> Work is in an external dirty workspace</label>
      <button type="button" onClick={save} disabled={busy || !summary.trim() || !remaining.trim()}>Save progress</button>
    </div> : <p>Recording progress requires execution state and permission to write the card’s report.</p>}
  </section>;
}
