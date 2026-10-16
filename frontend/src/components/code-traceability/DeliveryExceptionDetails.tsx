import type { DeliveryEvidenceProjection } from '@/types/delivery-evidence';

/** Spec-owned exceptions are not Card implementation records or proof. */
export function DeliveryExceptionDetails({ data, recordId }: { data: DeliveryEvidenceProjection; recordId: string }) {
  const records = data.records.filter(record => record.id === recordId);
  if (records.length !== 1 || records[0].kind !== 'waiver')
    return <p role="alert">The referenced delivery exception is unavailable in this Spec scope.</p>;
  const record = records[0];
  const disposition = record.legacy_disposition_unspecified ? 'Unspecified in the original record'
    : record.disposition || record.payload.disposition || 'Not specified';
  return <article aria-label="Delivery exception detail" data-readiness-ref={`exception:${record.id}`} tabIndex={-1}
    className="space-y-3 rounded-lg border border-amber-300 p-3 text-sm dark:border-amber-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-sky-500">
    <h3 className="font-semibold">Delivery exception · {record.revoked ? 'Revoked' : 'Recorded'}</h3>
    <p className="break-all">{record.id} · Edition {data.edition} · {record.actor_id} · {new Date(record.created_at).toLocaleString()}</p>
    <p className="text-amber-700 dark:text-amber-400">An exception is not implementation or verification proof. Its current effect is evaluated by the delivery gate.</p>
    <dl className="space-y-2">
      <div><dt className="font-medium">Phase</dt><dd>{record.payload.phase || 'Not specified'}</dd></div>
      <div><dt className="font-medium">Disposition</dt><dd className="break-words">{disposition}</dd></div>
      <div><dt className="font-medium">Justification</dt><dd className="whitespace-pre-wrap break-words">{record.payload.justification}</dd></div>
    </dl>
    <details className="rounded border border-amber-200 p-2 text-xs dark:border-amber-800"><summary className="cursor-pointer">Recorded obligation scope</summary>
      {record.payload.bindings?.length ? <ul className="mt-2 space-y-2">{record.payload.bindings.map(binding =>
        <li key={binding.obligation_ref} className="break-all">{binding.obligation_ref} · {binding.semantic_sha256}</li>)}</ul>
        : <p className="mt-2">Scope details are unavailable in this projection.</p>}
    </details>
  </article>;
}
