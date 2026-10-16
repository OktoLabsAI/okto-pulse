import { useEffect, useState } from 'react';
import { useDashboardApi } from '@/services/api';
import type { DeliveryPerCard, DeliverySelectionInput } from '@/types/delivery-evidence';

export function DeliverySelectionEditor({ boardId, cardId, specId, onChange, onPending }: {
  boardId: string; cardId: string; specId: string;
  onChange: (value: DeliverySelectionInput | undefined) => void;
  onPending: (value: boolean) => void;
}) {
  const api = useDashboardApi();
  const [enabled, setEnabled] = useState(false);
  const [recordSet, setRecordSet] = useState<DeliveryPerCard['selection']>();
  const [input, setInput] = useState<DeliverySelectionInput>();
  const [error, setError] = useState('');
  const [reload, setReload] = useState(0);
  useEffect(() => {
    let active = true;
    setError(''); setInput(undefined); setRecordSet(undefined); onChange(undefined);
    onPending(enabled);
    if (enabled) void api.getDeliveryEvidence(boardId, specId).then(projection => {
      if (!active) return;
      const card = projection.per_card?.find(row => row.card_id === cardId);
      if (!card?.selection || card.card_version === undefined || card.delivery_revision === undefined) {
        throw new Error('The current delivery selection is unavailable. Refresh before submitting.');
      }
      const value = { expected_card_version: card.card_version, expected_spec_edition: projection.edition,
        expected_delivery_revision: card.delivery_revision,
        record_ids: card.selection.truncated ? [] : card.selection.records.map(row => row.id) };
      setRecordSet(card.selection); setInput(value); onChange(value); onPending(false);
    }).catch(reason => { if (active) setError(reason instanceof Error ? reason.message : 'Selection could not be loaded.'); });
    return () => { active = false; };
  }, [api, boardId, cardId, specId, enabled, reload, onChange, onPending]);

  function toggle(identity: string, checked: boolean) {
    if (!input) return;
    const value = { ...input, record_ids: checked ? [...input.record_ids, identity] : input.record_ids.filter(id => id !== identity) };
    setInput(value); onChange(value);
  }
  function reuseImpact(checked: boolean) {
    if (!input) return;
    const value = { ...input, reuse_impact: checked };
    setInput(value); onChange(value);
  }
  return <section className="mt-3 space-y-2 rounded border p-3 text-sm" aria-label="Report evidence selection">
    <label><input type="checkbox" checked={enabled} onChange={event => { onPending(event.target.checked); setEnabled(event.target.checked); }} /> Seal recorded evidence with this report</label>
    {enabled && <>
      <p>The report keeps the selected records and the impact you present. Selection does not approve evidence or hide later changes, failures or revocations.</p>
      {error && <p role="alert">{error}</p>}
      <button type="button" onClick={() => setReload(value => value + 1)}>Refresh evidence selection</button>
      {recordSet?.truncated && <p>The list is limited to 200 of {recordSet.total} records. No records were selected automatically.</p>}
      {input && <p>Delivery revision {input.expected_delivery_revision} · {input.record_ids.length} selected</p>}
      {input && <label className="block"><input type="checkbox" checked={input.reuse_impact ?? false} onChange={event => reuseImpact(event.target.checked)} /> Use accumulated impact from the selected records</label>}
      {input?.reuse_impact && <p>The server composes the selected declarations and checks their source bases before submitting. Unresolved or stale declarations require reconciliation; no manual impact block is needed for a valid selection.</p>}
      <div className="max-h-48 overflow-auto">{recordSet?.records.map(record => <label key={record.id} className="block">
        <input type="checkbox" checked={input?.record_ids.includes(record.id) ?? false} onChange={event => toggle(record.id, event.target.checked)} /> {record.kind}: {record.summary}
      </label>)}</div>
    </>}
  </section>;
}
