import type { TestScenario } from '@/types';
import { VerificationReportDetails } from './VerificationReportDetails';

const observedValue = (value: unknown) => typeof value === 'string' ? value : JSON.stringify(value);

/** Read the exact native evidence pointer. A replaced receipt cannot select a newer run. */
export function ScenarioReceiptDetails({ scenarios, receipt }: { scenarios: TestScenario[]; receipt: string }) {
  const matches = scenarios.filter(scenario => scenario.evidence?.execution_receipt === receipt);
  if (matches.length !== 1) return <p role="alert">The referenced test receipt is unavailable or ambiguous in the current Spec. Refresh the diagnosis.</p>;
  const scenario = matches[0], evidence = scenario.evidence!;
  const attestation = evidence.execution_attestation;
  return <article aria-label="Test receipt detail" data-readiness-ref={`receipt:${receipt}`} tabIndex={-1}
    className="space-y-3 rounded-lg border border-gray-200 p-3 text-sm dark:border-gray-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-sky-500">
    <h3 className="font-semibold">{scenario.title}</h3>
    <p className="break-all">Scenario: {scenario.id} · Status: {scenario.status}</p>
    <p>Verification method: {scenario.verification_method || 'Not specified'} · Evidence: {evidence.evidence_class || 'Not specified'}</p>
    <p className="text-xs text-gray-500 dark:text-gray-400">Stored submission for this exact receipt. Authentication, current verification and independent approval remain separate assessments.</p>
    <details className="rounded border p-2 text-xs"><summary className="cursor-pointer">Receipt identifier</summary><p className="mt-2 break-all font-mono">{receipt}</p></details>
    <VerificationReportDetails evidence={evidence} />
    <dl className="space-y-2">
      {evidence.test_run_id && <div><dt className="font-medium">Test run</dt><dd className="break-all">{evidence.test_run_id}</dd></div>}
      {evidence.last_run_at && <div><dt className="font-medium">Last run</dt><dd>{evidence.last_run_at}</dd></div>}
      {evidence.test_file_path && <div><dt className="font-medium">Test</dt><dd className="break-all">{evidence.test_file_path}{evidence.test_function ? ` · ${evidence.test_function}` : ''}</dd></div>}
      {evidence.manifest_ref && <div><dt className="font-medium">Manifest</dt><dd className="break-all">{evidence.manifest_ref}</dd></div>}
      {evidence.replay_command && <div><dt className="font-medium">Recorded replay command</dt><dd className="whitespace-pre-wrap break-all font-mono text-xs">{evidence.replay_command}</dd></div>}
      {evidence.output_snippet && <div><dt className="font-medium">Recorded output</dt><dd className="whitespace-pre-wrap break-words font-mono text-xs">{evidence.output_snippet}</dd></div>}
    </dl>
    {attestation && <details className="rounded border p-2 text-xs"><summary className="cursor-pointer">Execution attestation</summary>
      <dl className="mt-2 space-y-2">
        <div><dt className="font-medium">Run</dt><dd className="break-all">{attestation.run_id} · {attestation.executed_at} · {attestation.outcome}</dd></div>
        <div><dt className="font-medium">Producer</dt><dd className="break-words">{attestation.provenance.producer} · {attestation.provenance.producer_version} · {attestation.provenance.adapter} · {attestation.provenance.environment}</dd></div>
        {attestation.execution_basis && <div><dt className="font-medium">Source basis</dt><dd className="break-all">{attestation.execution_basis.source_ref} · {attestation.execution_basis.revision} · {attestation.execution_basis.runner_ref}</dd></div>}
        <div><dt className="font-medium">Product runtime exercised (recorded)</dt><dd>{attestation.product_runtime_exercised ? 'Yes' : 'No'}</dd></div>
      </dl>
      <div className="mt-3 space-y-2">{attestation.assertions.map((assertion, index) => <details key={index} className="rounded border p-2">
        <summary className="cursor-pointer break-words">{assertion.name} · {assertion.status}</summary>
        <dl className="mt-2 space-y-2">
          <div><dt className="font-medium">Expected</dt><dd className="whitespace-pre-wrap break-all">{observedValue(assertion.expected)}</dd></div>
          <div><dt className="font-medium">Observed</dt><dd className="whitespace-pre-wrap break-all">{observedValue(assertion.observed)}</dd></div>
          {assertion.message && <div><dt className="font-medium">Message</dt><dd className="whitespace-pre-wrap break-words">{assertion.message}</dd></div>}
        </dl>
      </details>)}</div>
    </details>}
  </article>;
}
