import { useEffect, useRef, useState } from 'react';
import { useDashboardApi } from '@/services/api';
import type { TestScenario } from '@/types';
import { AuthenticatedFetchError } from '@/lib/authFetch';

const methods = ['automated_test', 'static_analysis', 'inspection', 'demonstration'] as const;
type Method = typeof methods[number];
type Props = { boardId: string; specId: string; version: number; scenario: TestScenario; canEdit: boolean; readCurrent?: boolean; missing?: boolean; onDraftChange?: (dirty: boolean) => void; onSaved: () => Promise<void> };

export function ScenarioVerificationMethodEditor(props: Props) {
  return <MethodEditor key={JSON.stringify([props.boardId, props.specId, props.scenario.id, props.canEdit])} {...props} />;
}

function MethodEditor({ boardId, specId, version, scenario, canEdit, readCurrent = true, missing = false, onDraftChange, onSaved }: Props) {
  const api = useDashboardApi();
  const [draft, setDraft] = useState<{ method: string; version: number } | null>(null);
  const method = draft?.method ?? scenario.verification_method ?? '';
  const [conflictVersion, setConflictVersion] = useState<number | null>(null);
  const baseChanged = !!draft && draft.version !== version;
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState('');
  const live = useRef(true);
  const writing = useRef(false);
  useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);
  useEffect(() => { onDraftChange?.(draft !== null); }, [draft, onDraftChange]);
  async function reload() {
    try { await onSaved(); } catch { if (live.current) setError('Saved. Reload the Spec to see the current method.'); }
  }
  async function save() {
    if (!canEdit || missing || !readCurrent || writing.current || saved || baseChanged || conflictVersion !== null || (method && !methods.includes(method as Method))) return;
    const expectedVersion = draft?.version ?? version;
    setDraft(current => current ?? { method, version: expectedVersion });
    writing.current = true; setBusy(true); setError('');
    try {
      await api.updateScenarioVerificationMethod(boardId, specId, scenario.id, (method || null) as Method | null, expectedVersion);
      if (!live.current) return;
      setSaved(true); await reload();
    } catch (cause) {
      if (live.current) {
        setError(cause instanceof Error ? cause.message : 'Method could not be saved. Refresh the Spec before retrying.');
        // This endpoint returns the native code as HTTP detail text. Other 409s
        // include locks and workflow refusals and must not imply a version change.
        if (cause instanceof AuthenticatedFetchError && cause.status === 409 && cause.message === 'spec_version_conflict') {
          setDraft(current => current ?? { method, version: expectedVersion });
          setConflictVersion(expectedVersion);
        }
      }
    } finally { writing.current = false; if (live.current) setBusy(false); }
  }
  if (!canEdit) return <p className="text-xs text-gray-500">Verification method: {scenario.verification_method || 'Not defined'}</p>;
  return <div className="space-y-2 rounded border border-gray-300 p-2 dark:border-gray-600">
    {missing && <p role="status" className="text-xs text-amber-600">This scenario was removed. Your selected method is preserved for reference; it cannot be saved or recreate the scenario.</p>}
    {!readCurrent && <p role="status" className="text-xs text-amber-600">Current Spec could not be verified. Your selection is preserved; refresh before saving.</p>}
    {!missing && !saved && (baseChanged || conflictVersion !== null) && <div className="rounded-lg border border-amber-300 p-2 text-xs">
      <p>The Spec changed. Review the current scenario before saving your selected method.</p>
      <p>Current saved method: {scenario.verification_method || 'Not defined'}</p>
      <p className="whitespace-pre-wrap">Expected observation: {scenario.then}</p>
      <button type="button" className="btn btn-secondary text-xs mt-2"
        disabled={busy || missing || !readCurrent || (conflictVersion !== null && conflictVersion === version)}
        onClick={() => { setDraft({ method, version }); setConflictVersion(null); setError(''); }}>Review method base</button>
    </div>}
    <label className="text-xs">Verification method
      <select aria-label={`Verification method ${scenario.id}`} disabled={busy || saved || missing} value={method}
        onChange={event => setDraft(current => ({ method: event.target.value, version: current?.version ?? version }))} className="ml-2 rounded bg-white p-1 dark:bg-gray-800">
        <option value="">Not defined</option>
        {method && !methods.includes(method as Method) && <option value={method} disabled>Unsupported stored method: {method}</option>}
        {methods.map(value => <option key={value} value={value}>{value.replace(/_/g, ' ')}</option>)}
      </select>
    </label>
    <p className="text-xs text-gray-500">A method declares the required observation. Credit requires an installed admission path and authenticated evidence; unsupported methods remain pending.</p>
    {(scenario.evidence) && <p className="text-xs text-amber-600">Changing this method invalidates the current scenario evidence.</p>}
    <button type="button" className="btn btn-primary text-xs disabled:opacity-50 disabled:cursor-not-allowed" disabled={busy || saved || missing || !readCurrent || baseChanged || conflictVersion !== null || (Boolean(method) && !methods.includes(method as Method))} onClick={() => void save()}>{busy ? 'Saving method…' : 'Save verification method'}</button>
    {error && <p role="alert">{error}</p>}
    {saved && <button type="button" className="btn btn-secondary text-xs" onClick={() => void reload()}>Reload saved method</button>}
    {saved && <button type="button" className="btn btn-secondary text-xs ml-2" disabled={busy || missing || !readCurrent || !draft || version <= draft.version}
      onClick={() => { setDraft(null); setSaved(false); setError(''); setConflictVersion(null); }}>Edit current method</button>}
  </div>;
}
