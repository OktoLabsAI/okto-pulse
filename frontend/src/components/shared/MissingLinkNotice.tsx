import type { MissingLinkContext } from '@/types';

export function MissingLinkNotice({ context }: { context?: MissingLinkContext | null }) {
  if (!context || context.status === 'not_authorized') return null;
  if (context.status === 'unavailable') {
    return <p role="status" className="text-xs text-amber-700 dark:text-amber-300">
      Current reference checks are unavailable.
      {context.mode === 'blocking' && ' Completion requires a successful check. Retry after the source is available.'}
    </p>;
  }
  if (context.finding_count === 0) return null;
  return <section aria-label="Missing reference checks" className="mb-3 rounded border border-amber-300 p-3 text-sm dark:border-amber-700">
    <p className="font-medium">{context.finding_count} unresolved declared reference(s)</p>
    <p>{context.mode === 'blocking' ? 'Correct these references before completion.' : 'Advisory: these references do not block completion.'}</p>
    <ul className="mt-2 space-y-2">
      {context.findings.map((finding) => <li key={`${finding.source_ref}:${finding.field}:${finding.target_ref}`}>
        <p>{finding.reason === 'target_ambiguous' ? 'The target ID is ambiguous.' : 'The target is absent from the permitted scope.'}</p>
        <p className="break-all text-xs">Source: {finding.source_ref} · Field: {finding.field}</p>
        <p className="break-all text-xs">Reference: {finding.target_ref}</p>
        <p className="text-xs">{finding.correction_operation === 'update_card'
          ? 'Review this Card’s declared links.' : 'Review the declared links in the Spec. If locked, use the authorized revision workflow.'}</p>
      </li>)}
    </ul>
    {context.truncated && <p className="mt-2 text-xs">Showing a bounded selection. Review the remaining declared links in this artifact.</p>}
  </section>;
}
