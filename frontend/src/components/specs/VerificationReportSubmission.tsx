import { useEffect, useRef, useState } from 'react';
import { useDashboardApi } from '@/services/api';
import type { Spec, TestScenario } from '@/types';
import { AuthenticatedFetchError } from '@/lib/authFetch';

type Props = { specId: string; scenario: TestScenario; allowedResults: string[]; canSubmit: boolean;
  readCurrent?: boolean; missing?: boolean; onDraftChange?: (dirty: boolean) => void; onReload: () => Promise<Spec>;
  onSaved: () => Promise<void>; onRejected: (error: unknown, status: string) => Promise<unknown> };

export function VerificationReportSubmission(props: Props) {
  return <ReportForm key={JSON.stringify([props.specId, props.scenario.id, props.canSubmit])} {...props} />;
}

function ReportForm({ specId, scenario, allowedResults, canSubmit, readCurrent = true, missing = false, onDraftChange, onReload, onSaved, onRejected }: Props) {
  const api = useDashboardApi();
  const [raw, setRaw] = useState('');
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState('');
  const [base, setBase] = useState<string | null>(null);
  const currentBase = JSON.stringify(scenario);
  const baseChanged = Boolean(raw && base !== currentBase);
  const currentBaseRef = useRef(currentBase);
  currentBaseRef.current = currentBase;
  const missingRef = useRef(missing);
  missingRef.current = missing;
  const [uncertain, setUncertain] = useState<{ receipt: string | null; result: string } | null>(null);
  const [checked, setChecked] = useState(false);
  const live = useRef(true);
  const writing = useRef(false);
  useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);
  useEffect(() => { onDraftChange?.(Boolean(raw)); }, [raw, onDraftChange]);
  const available = ['inspection', 'static_analysis', 'demonstration'].includes(scenario.verification_method || '')
    && allowedResults.some(result => ['passed', 'failed', 'ready'].includes(result));
  if (!canSubmit || (!available && !raw)) return null;
  async function checkSubmission() {
    if (writing.current || !uncertain) return;
    writing.current = true; setBusy(true); setError('');
    try {
      const current = await onReload();
      if (!live.current) return;
      if (current.id !== specId) throw new Error('The report read does not match its Spec.');
      const found = current.test_scenarios?.find(item => item.id === scenario.id);
      if (uncertain.receipt && found?.evidence?.execution_receipt === uncertain.receipt && found.status === uncertain.result) {
        setSaved(true); setUncertain(null);
      } else { setChecked(true); setError('The requested result was not confirmed. Inspect the current scenario before discarding this unresolved submission.'); }
    } catch (cause) { if (live.current) setError(cause instanceof Error ? cause.message : 'Could not check the report.'); }
    finally { writing.current = false; if (live.current) setBusy(false); }
  }
  async function submit() {
    if (writing.current || saved || uncertain || missing || !readCurrent || !available || baseChanged) return;
    writing.current = true; setBusy(true); setError('');
    let result = '';
    let stage: 'input' | 'admission' | 'status' = 'input';
    let receipt: string | null = null;
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
      stage = 'admission';
      const admitted = await api.admitTestVerificationReport(specId, scenario.id, report);
      if (!live.current) return;
      receipt = admitted.evidence.execution_receipt || null;
      if (missingRef.current || currentBaseRef.current !== currentBase) {
        stage = 'input';
        throw new Error('The scenario changed during admission. Review the current scenario before recording a result.');
      }
      stage = 'status';
      await api.updateTestScenarioStatus(specId, scenario.id, { status: result as 'ready' | 'passed' | 'failed', evidence: admitted.evidence });
      if (!live.current) return;
      setSaved(true);
      try { await onSaved(); } catch { if (live.current) setError('Result saved. Reload the Spec to see the current evidence.'); }
    } catch (cause) {
      if (live.current) {
        setError(cause instanceof Error ? cause.message : 'Report could not be recorded.');
        const rejected = cause instanceof AuthenticatedFetchError && cause.status >= 400 && cause.status < 500 && cause.status !== 408;
        if (stage !== 'input' && !rejected) { setUncertain({ receipt, result }); setChecked(false); }
        if (result) await onRejected(cause, result);
      }
    } finally { writing.current = false; if (live.current) setBusy(false); }
  }
  return <div className="space-y-2 rounded border p-3">
    {missing && <div role="status" className="text-xs text-amber-600">
      <p>This scenario was removed. The report draft is preserved for reference and cannot be submitted.</p>
      <button type="button" className="btn btn-secondary text-xs" disabled={busy} onClick={() => { setRaw(''); setBase(null); setUncertain(null); }}>Discard removed report draft</button>
    </div>}
    {!missing && baseChanged && !saved && <div role="status" className="text-xs text-amber-600">
      <p>The scenario changed. Your report draft is preserved. Review its current method and expected observation before submitting.</p>
      <p>Current method: {scenario.verification_method || 'Not defined'}. Expected observation: {scenario.then}</p>
      <button type="button" className="btn btn-secondary text-xs" disabled={busy || Boolean(uncertain) || !readCurrent || !available}
        onClick={() => { setBase(currentBase); setError(''); }}>Review report base</button>
    </div>}
    {!readCurrent && <p role="status" className="text-xs text-amber-600">Current scenario is unavailable. Your report draft is preserved.</p>}
    {uncertain && !missing && <div role="status" className="text-xs text-amber-600">
      <p>The response was lost. Check the current scenario; admission and recording will not be repeated automatically.</p>
      <button type="button" className="btn btn-secondary text-xs" disabled={busy} onClick={() => void checkSubmission()}>Check report submission</button>
      {checked && <button type="button" className="btn btn-secondary text-xs" disabled={busy || !readCurrent}
        onClick={() => { setUncertain(null); setRaw(''); setBase(null); setError(''); setChecked(false); }}>Discard unresolved submission</button>}
    </div>}
    <p className="text-xs">Record an external {scenario.verification_method?.replace(/_/g, ' ')} report. Include versioned sources, observations for every linked criterion and the result. Inconclusive, aborted and unavailable results keep the scenario ready for another attempt. Submission does not approve the Test Card.</p>
    <label className="block text-xs">Verification report (JSON)
      <textarea aria-label={`Verification report ${scenario.id}`} value={raw} disabled={busy || saved || missing || Boolean(uncertain)}
        onChange={event => { setRaw(event.target.value); setBase(value => value ?? currentBase); }} rows={5} className="mt-1 w-full rounded border p-2 dark:bg-gray-900" />
    </label>
    <button type="button" className="btn btn-primary text-xs disabled:opacity-50 disabled:cursor-not-allowed" disabled={busy || saved || missing || Boolean(uncertain) || baseChanged || !readCurrent || !available || !raw.trim()} onClick={() => void submit()}>{busy ? 'Recording report…' : saved ? 'Report recorded' : 'Record verification report'}</button>
    {saved && <button type="button" className="btn btn-secondary text-xs" onClick={() => { void onSaved().catch(() => setError('Result saved. Reload the Spec to see the current evidence.')); }}>Reload saved report</button>}
    {error && <p role="alert">{error}</p>}
  </div>;
}
