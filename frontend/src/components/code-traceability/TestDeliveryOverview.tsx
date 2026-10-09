import type { DeliveryEvidenceProjection } from '@/types/delivery-evidence';

/** Presents recorded evidence, never a locally inferred delivery verdict. */
export function TestDeliveryOverview({ data, cardId }: { data: DeliveryEvidenceProjection; cardId: string }) {
  const runs = data.candidates.filter(row => row.kind === 'test' && row.card_id === cardId);
  const outcomes = (data.tests ?? []).filter(row => row.card_id === cardId);
  const current = outcomes.filter(row => row.current_verified_run);
  return <div className="space-y-4">
    <section className="rounded-xl border border-cyan-200 bg-cyan-50/50 p-5 dark:border-cyan-900 dark:bg-cyan-950/20" aria-label="Test delivery guide">
      <h3 className="text-base font-semibold text-gray-900 dark:text-gray-100">Evidence of test execution</h3>
      <p className="mt-1 text-sm text-gray-600 dark:text-gray-300">Connect a test run to the requirements and implementation it verified. This is where results become traceable delivery evidence.</p>
      <ol className="mt-4 grid gap-3 sm:grid-cols-3">
        {[
          ['Execute', 'Run the scenarios linked to this card and save authenticated results.'],
          ['Connect', 'Select the run, the obligations checked and the implementation observed.'],
          ['Record', 'Explain the outcome, including failures. Review the combined coverage in the Spec.'],
        ].map(([title, description], index) => <li key={title} className="rounded-lg border border-cyan-100 bg-white/80 p-3 dark:border-cyan-900/60 dark:bg-gray-900/60">
          <span className="text-xs font-semibold text-cyan-700 dark:text-cyan-400">{index + 1}. {title}</span>
          <p className="mt-1 text-xs leading-relaxed text-gray-600 dark:text-gray-400">{description}</p>
        </li>)}
      </ol>
    </section>
    <section aria-label="Recorded test outcomes" className="overflow-hidden rounded-xl border border-gray-200 dark:border-gray-800">
      <div className="flex flex-wrap items-center justify-between gap-2 bg-gray-50 px-4 py-3 dark:bg-gray-900/50">
        <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100">Recorded test outcomes</h3>
        <span className="text-xs text-gray-500 dark:text-gray-400">{outcomes.length} recorded · {current.length} current</span>
      </div>
      {outcomes.length === 0 ? <div className="p-5 text-sm">
        <p className="font-medium text-gray-800 dark:text-gray-200">No test evidence recorded yet</p>
        <p className="mt-1 text-gray-500 dark:text-gray-400">{runs.length ? 'A run is available. Use Record test evidence to connect its result to the work verified.' : 'Start by executing a scenario linked to this Test Card. Then return here to record what the run verified.'}</p>
      </div> : <div className="divide-y divide-gray-100 dark:divide-gray-800">
        {outcomes.slice(-20).reverse().map(row => {
          const record = data.records.find(item => item.id === row.id);
          const label = (row.current_verified_run && runs.find(run => run.id === row.scenario_id)?.label) || row.scenario_id;
          return <details key={row.id} className="group">
            <summary className="flex cursor-pointer list-none flex-wrap items-center gap-3 px-4 py-3 text-sm hover:bg-gray-50 dark:hover:bg-gray-800/40">
              <span aria-hidden="true" className="text-gray-400 transition-transform group-open:rotate-90">›</span>
              <span className="min-w-0 flex-1 break-words font-medium text-gray-800 dark:text-gray-200">{label}</span>
              <span className={`rounded-full px-2 py-1 text-xs font-medium ${row.result === 'passed' ? 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300' : row.result === 'failed' ? 'bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300' : 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300'}`}>{row.result}</span>
              <span className="text-xs text-gray-500">{row.current_verified_run ? 'Current run' : 'Not current'}</span>
            </summary>
            <div className="space-y-2 border-t border-gray-100 bg-gray-50/50 px-5 py-3 text-sm dark:border-gray-800 dark:bg-gray-900/30">
              <p className="text-gray-600 dark:text-gray-400">{row.current_verified_run ? 'Current authenticated run' : 'Outside the current authenticated run'}</p>
              {record?.payload.justification && <p className="whitespace-pre-wrap text-gray-800 dark:text-gray-200">{record.payload.justification}</p>}
              <dl className="grid gap-2 text-xs text-gray-500 sm:grid-cols-2">
                <div><dt className="font-medium">Scenario</dt><dd className="break-all">{row.scenario_id}</dd></div>
                <div><dt className="font-medium">Evidence record</dt><dd className="break-all">{row.id}</dd></div>
                {record && <><div><dt className="font-medium">Recorded by</dt><dd>{record.actor_id}</dd></div><div><dt className="font-medium">Recorded at</dt><dd>{record.created_at}</dd></div></>}
              </dl>
            </div>
          </details>;
        })}
      </div>}
      <div className="space-y-1 border-t border-gray-100 px-4 py-3 text-xs text-gray-500 dark:border-gray-800 dark:text-gray-400">
        {outcomes.length > 20 && <p>Showing the latest 20 of {outcomes.length} recorded results in this view.</p>}
        <p>Recording a result does not approve delivery. Coverage requires current passing evidence for the required criteria and completed cards.</p>
        <details><summary className="cursor-pointer font-medium">How this affects delivery</summary><p className="mt-2">Test Cards are excluded from the implementation DoD gate. Authenticated test outcomes contribute to the Spec rollup; other validation requirements still apply.</p></details>
      </div>
    </section>
  </div>;
}
