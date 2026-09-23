import { useEffect, useRef, useState } from 'react';
import { useDashboardApi } from '@/services/api';
import type { TestScenario } from '@/types';

type Props = { specId: string; scenario: TestScenario; allowedResults: string[]; canSubmit: boolean;
  onSaved: () => Promise<void>; onRejected: (error: unknown, status: string) => Promise<unknown> };

export function VerificationReportSubmission(props: Props) {
  return <ReportForm key={JSON.stringify([props.specId, props.scenario, props.canSubmit, props.allowedResults])} {...props} />;
}

function ReportForm({ specId, scenario, allowedResults, canSubmit, onSaved, onRejected }: Props) {
  const api = useDashboardApi();
  const [raw, setRaw] = useState('');
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState('');
  const live = useRef(true);
  const writing = useRef(false);
  useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);
  if (!canSubmit || !['inspection', 'static_analysis', 'demonstration'].includes(scenario.verification_method || '')
    || !allowedResults.some(result => ['passed', 'failed', 'ready'].includes(result))) return null;
  async function submit() {
    if (writing.current || saved) return;
    writing.current = true; setBusy(true); setError('');
    let result = '';
    try {
      if (new TextEncoder().encode(raw).length > 65536) throw new Error('Report exceeds 64 KiB.');
      const report = JSON.parse(raw) as Record<string, unknown>;
      if (!report || Array.isArray(report) || report.method !== scenario.verification_method
        || typeof report.result !== 'string'
        || !['passed', 'failed', 'inconclusive', 'aborted', 'unavailable'].includes(report.result)) {
        throw new Error('Use a report for this verification method and an available result.');
      }
      result = ['passed', 'failed'].includes(report.result) ? report.result : 'ready';
      if (!allowedResults.includes(result)) throw new Error('The required scenario transition is not available.');
      const admitted = await api.admitTestVerificationReport(specId, scenario.id, report);
      if (!live.current) return;
      await api.updateTestScenarioStatus(specId, scenario.id, { status: result as 'ready' | 'passed' | 'failed', evidence: admitted.evidence });
      if (!live.current) return;
      setSaved(true);
      try { await onSaved(); } catch { if (live.current) setError('Result saved. Reload the Spec to see the current evidence.'); }
    } catch (cause) {
      if (live.current) {
        setError(cause instanceof Error ? cause.message : 'Report could not be recorded.');
        if (result) await onRejected(cause, result);
      }
    } finally { writing.current = false; if (live.current) setBusy(false); }
  }
  return <div className="space-y-2 rounded border p-3">
    <p className="text-xs">Record an external {scenario.verification_method?.replace(/_/g, ' ')} report. Include versioned sources, observations for every linked criterion and the result. Inconclusive, aborted and unavailable results keep the scenario ready for another attempt. Submission does not approve the Test Card.</p>
    <label className="block text-xs">Verification report (JSON)
      <textarea aria-label={`Verification report ${scenario.id}`} value={raw} disabled={busy || saved}
        onChange={event => setRaw(event.target.value)} rows={5} className="mt-1 w-full rounded border p-2 dark:bg-gray-900" />
    </label>
    <button type="button" disabled={busy || saved || !raw.trim()} onClick={() => void submit()}>{busy ? 'Recording report…' : saved ? 'Report recorded' : 'Record verification report'}</button>
    {error && <p role="alert">{error}</p>}
  </div>;
}
