import { useEffect, useRef, useState } from 'react';
import { useDashboardApi } from '@/services/api';
import type { TestScenario } from '@/types';

const methods = ['automated_test', 'static_analysis', 'inspection', 'demonstration'] as const;
type Method = typeof methods[number];
type Props = { boardId: string; specId: string; version: number; scenario: TestScenario; canEdit: boolean; onSaved: () => Promise<void> };

export function ScenarioVerificationMethodEditor(props: Props) {
  return <MethodEditor key={JSON.stringify([props.boardId, props.specId, props.version, props.scenario.id, props.scenario.verification_method, props.canEdit])} {...props} />;
}

function MethodEditor({ boardId, specId, version, scenario, canEdit, onSaved }: Props) {
  const api = useDashboardApi();
  const [method, setMethod] = useState(scenario.verification_method || '');
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState('');
  const live = useRef(true);
  const writing = useRef(false);
  useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);
  async function reload() {
    try { await onSaved(); } catch { if (live.current) setError('Saved. Reload the Spec to see the current method.'); }
  }
  async function save() {
    if (!canEdit || writing.current || saved || (method && !methods.includes(method as Method))) return;
    writing.current = true; setBusy(true); setError('');
    try {
      await api.updateScenarioVerificationMethod(boardId, specId, scenario.id, (method || null) as Method | null, version);
      if (!live.current) return;
      setSaved(true); await reload();
    } catch (cause) {
      if (live.current) setError(cause instanceof Error ? cause.message : 'Method could not be saved. Refresh the Spec before retrying.');
    } finally { writing.current = false; if (live.current) setBusy(false); }
  }
  if (!canEdit) return <p className="text-xs text-gray-500">Verification method: {scenario.verification_method || 'Not defined'}</p>;
  return <div className="space-y-2 rounded border border-gray-300 p-2 dark:border-gray-600">
    <label className="text-xs">Verification method
      <select aria-label={`Verification method ${scenario.id}`} disabled={busy || saved} value={method} onChange={event => setMethod(event.target.value)} className="ml-2 rounded bg-white p-1 dark:bg-gray-800">
        <option value="">Not defined</option>
        {method && !methods.includes(method as Method) && <option value={method} disabled>Unsupported stored method: {method}</option>}
        {methods.map(value => <option key={value} value={value}>{value.replace(/_/g, ' ')}</option>)}
      </select>
    </label>
    <p className="text-xs text-gray-500">A method declares the required observation. Credit requires an installed admission path and authenticated evidence; unsupported methods remain pending.</p>
    {(scenario.evidence || scenario.latest_evidence) && <p className="text-xs text-amber-600">Changing this method invalidates the current scenario evidence.</p>}
    <button type="button" disabled={busy || saved || (Boolean(method) && !methods.includes(method as Method))} onClick={() => void save()}>{busy ? 'Saving method…' : 'Save verification method'}</button>
    {error && <p role="alert">{error}</p>}
    {saved && <button type="button" onClick={() => void reload()}>Reload saved method</button>}
  </div>;
}
