import { CardDeliveryDoDPanel } from './CardDeliveryDoDPanel';
import { sameDeliveryBasis } from './deliveryReportDraft';
import type { CardDeliveryBatchDraft, DeliverySelectionInput } from '@/types/delivery-evidence';

export function DeliveryReportBatchEditor({ boardId, card, selection, draft, onChange, canProgress, canRecord, canTest }: {
  boardId: string; card: { id: string; card_type: string; spec_id: string };
  selection: DeliverySelectionInput; draft?: CardDeliveryBatchDraft;
  onChange: (value: CardDeliveryBatchDraft | undefined) => void;
  canProgress: boolean; canRecord: boolean; canTest: boolean;
}) {
  function stage(value: CardDeliveryBatchDraft) {
    if (!sameDeliveryBasis(value, selection) || (draft && !sameDeliveryBasis(draft, selection))) {
      throw new Error('The evidence changed while this report was being prepared. Refresh the selection and review the draft before submitting.');
    }
    const entries = [...(draft?.entries ?? []), ...value.entries.map(entry => ({ ...entry, client_ref: crypto.randomUUID() }))];
    if (entries.length > 50 || entries.length + selection.record_ids.length > 200) {
      throw new Error('A report accepts up to 50 new entries and 200 selected records in total.');
    }
    const next = { ...value, entries };
    if (new TextEncoder().encode(JSON.stringify(next)).length > 128 * 1024) {
      throw new Error('The draft exceeds the report size limit. Reduce its content before submitting.');
    }
    onChange(next);
  }
  return <section aria-label="Last delivery batch" className="space-y-3 rounded border p-3 text-sm">
    <p>These entries are drafts. They are saved together with the report only when you submit it. A rejected report saves none of them. Closing this dialog discards unsent entries.</p>
    {draft && !sameDeliveryBasis(draft, selection) && <p role="alert">The selected evidence and draft have different versions. Remove the draft entries and prepare them against the refreshed selection.</p>}
    <ol aria-label="Unsent delivery entries">
      {draft?.entries.map((entry, index) => <li key={entry.client_ref} className="border-b py-2">
        <span>{entry.kind}: {entry.justification}</span>
        <button type="button" className="ml-2 underline" aria-label={`Remove draft entry ${index + 1}`} onClick={() => {
          const entries = draft.entries.filter(row => row.client_ref !== entry.client_ref);
          onChange(entries.length ? { ...draft, entries } : undefined);
        }}>Remove</button>
      </li>)}
    </ol>
    <CardDeliveryDoDPanel boardId={boardId} card={card} canProgress={canProgress} canRecord={canRecord} canTest={canTest} onStage={stage} />
  </section>;
}
