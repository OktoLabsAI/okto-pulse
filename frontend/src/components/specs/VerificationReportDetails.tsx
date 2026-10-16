import type { TestScenarioEvidence } from '@/types';

export function VerificationReportDetails({ evidence }: { evidence?: TestScenarioEvidence | null }) {
  if (evidence?.evidence_class !== 'verification_report') return null;
  const report = evidence.verification_report;
  return <details className="rounded border p-2 text-xs">
    <summary>External verification report</summary>
    <p>Submission author: {evidence.report_author_id || 'Unknown'}</p>
    <p>{evidence.execution_receipt ? 'Submission receipt attached. Independent approval is separate.' : 'Missing submission receipt — unverified.'}</p>
    {/* Keep arbitrary historical extra fields readable without inferring validity. */}
    <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap">{JSON.stringify(report ?? null, null, 2)}</pre>
  </details>;
}
