import { useEffect, useRef, useState } from 'react';
import { useDashboardApi } from '@/services/api';

export type VerificationProfile = 'functional' | 'integration' | 'technical' | 'operational';
export type VerificationRequirementType = 'functional_requirement' | 'technical_requirement' | 'integration_requirement' | 'observability_requirement' | 'business_rule';
export interface CriterionRequirementLink {
  requirement_type: VerificationRequirementType;
  requirement_id: string;
  aspect?: string | null;
}
export interface VerificationRequirementOption {
  type: VerificationRequirementType;
  id: string;
  title: string;
}
interface Criterion {
  id: string;
  text: string;
  verification_profile?: VerificationProfile | null;
  requirement_links?: CriterionRequirementLink[] | null;
}
const profiles: VerificationProfile[] = ['functional', 'integration', 'technical', 'operational'];
const types: VerificationRequirementType[] = ['functional_requirement', 'technical_requirement', 'integration_requirement', 'observability_requirement', 'business_rule'];
const identity = (type: string, id: string) => JSON.stringify([type, id]);


function editableCriterion(value: unknown): Criterion | null {
  if (!value || typeof value !== 'object') return null;
  const row = value as Record<string, unknown>;
  if (typeof row.id !== 'string' || !row.id || (row.status != null && row.status !== 'active')) return null;
  if (row.verification_profile != null && !profiles.includes(row.verification_profile as VerificationProfile)) return null;
  if (row.requirement_links != null && (!Array.isArray(row.requirement_links) || row.requirement_links.some(link => (
    !link || typeof link !== 'object' || !types.includes(link.requirement_type)
    || typeof link.requirement_id !== 'string' || !link.requirement_id.trim()
    || (link.aspect != null && (typeof link.aspect !== 'string' || !link.aspect.trim()))
    || Object.keys(link).some(key => !['requirement_type', 'requirement_id', 'aspect'].includes(key))
  )))) return null;
  if (Array.isArray(row.requirement_links) && (row.requirement_links.length > 100
    || new Set(row.requirement_links.map(link => identity(link.requirement_type, link.requirement_id))).size !== row.requirement_links.length)) return null;
  return { ...row, text: String(row.text || row.title || '') } as unknown as Criterion;
}

function CriterionEditor({ specId, version, criterion, options, onSaved }: {
  specId: string; version: number; criterion: Criterion;
  options: VerificationRequirementOption[]; onSaved: () => Promise<void>;
}) {
  const api = useDashboardApi();
  const [profile, setProfile] = useState<VerificationProfile | ''>(criterion.verification_profile || '');
  const [links, setLinks] = useState<CriterionRequirementLink[]>(() => (criterion.requirement_links || []).map(link => ({ ...link })));
  const [selection, setSelection] = useState('');
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState('');
  const inflight = useRef(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const available = options.filter(option => !links.some(link => identity(link.requirement_type, link.requirement_id) === identity(option.type, option.id)));

  async function refresh() {
    try { await onSaved(); } catch { if (mounted.current) setError('Saved. Refresh failed; reload the Spec to see the current version.'); }
  }
  async function save() {
    if (inflight.current || saved) return;
    inflight.current = true;
    setBusy(true); setError('');
    try {
      const result = await api.updateSpecEntity(specId, 'acceptance_criterion', criterion.id, {
        verification_profile: profile || null,
        requirement_links: links.map(link => ({ ...link, aspect: link.aspect?.trim() ? link.aspect : null })),
      }, version);
      if (!mounted.current) return;
      if (!result.success) throw new Error(result.error_message || 'The change was refused. Review the Spec and try again.');
      setSaved(true);
      await refresh();
    } catch (cause) {
      if (mounted.current) setError(cause instanceof Error ? cause.message : 'Could not save. Refresh the Spec before retrying.');
    } finally {
      inflight.current = false;
      if (mounted.current) setBusy(false);
    }
  }
  return <div className="space-y-3 rounded border border-slate-700 p-3">
    <fieldset disabled={busy || saved} className="space-y-3">
      <label className="block text-sm">Verification profile
        <select aria-label="Verification profile" className="ml-2 rounded bg-slate-800 p-1" value={profile} onChange={event => setProfile(event.target.value as VerificationProfile | '')}>
          <option value="">Not defined</option>
          {profiles.map(value => <option key={value} value={value}>{value}</option>)}
        </select>
      </label>
      <p className="text-xs text-slate-400">The criterion text states the observable condition. Links identify the obligations it covers; they do not establish verification results.</p>
      {links.map((link, index) => {
        const option = options.find(item => identity(item.type, item.id) === identity(link.requirement_type, link.requirement_id));
        return <div key={identity(link.requirement_type, link.requirement_id)} className="space-y-1 text-sm">
          <p>{option?.title || 'Requirement unavailable'} · {link.requirement_id}</p>
          <label className="block">Covered aspect (optional)
            <input aria-label={`Covered aspect ${index + 1}`} maxLength={2000} className="block w-full rounded bg-slate-800 p-2" value={link.aspect || ''} onChange={event => setLinks(links.map((item, i) => i === index ? { ...item, aspect: event.target.value } : item))} />
          </label>
          <button type="button" onClick={() => setLinks(links.filter((_, i) => i !== index))}>Remove link {index + 1}</button>
        </div>;
      })}
      <div className="flex gap-2">
        <select aria-label="Requirement to link" className="min-w-0 flex-1 rounded bg-slate-800 p-2" value={selection} onChange={event => setSelection(event.target.value)}>
          <option value="">Select a requirement</option>
          {available.map(option => <option key={identity(option.type, option.id)} value={identity(option.type, option.id)}>{option.type}: {option.title} ({option.id})</option>)}
        </select>
        <button type="button" disabled={!selection || links.length >= 100} onClick={() => {
          const option = available.find(item => identity(item.type, item.id) === selection);
          if (option) setLinks([...links, { requirement_type: option.type, requirement_id: option.id }]);
          setSelection('');
        }}>Add link</button>
      </div>
      <button type="button" onClick={() => void save()}>{busy ? 'Saving…' : 'Save verification plan'}</button>
    </fieldset>
    {error && <p role="alert">{error}</p>}
    {saved && <button type="button" onClick={() => void refresh()}>Reload saved criterion</button>}
  </div>;
}

export function CriterionVerificationPanel({ specId, version, criteria, options, canEdit, onSaved }: {
  specId: string; version: number; criteria: unknown[];
  options: VerificationRequirementOption[]; canEdit: boolean; onSaved: () => Promise<void>;
}) {
  const [openId, setOpenId] = useState<string | null>(null);
  const parsed = criteria.map(editableCriterion).filter((row): row is Criterion => row !== null);
  const rows = parsed.filter(row => parsed.filter(item => item.id === row.id).length === 1);
  const currentCount = criteria.filter(value => !value || typeof value !== 'object' || !('status' in value) || value.status == null || value.status === 'active').length;
  const unambiguousOptions = options.filter(option => options.filter(item => identity(item.type, item.id) === identity(option.type, option.id)).length === 1);
  return <section aria-label="Criterion verification" className="space-y-3">
    <h4 className="text-sm font-medium">Criterion verification</h4>
    <p className="text-xs text-slate-400">Define each criterion’s profile and requirement links. Planning completeness and evidence are evaluated separately.</p>
    {currentCount > rows.length && <p role="status">{currentCount - rows.length} criterion(s) have legacy or unsupported identity/metadata and require review.</p>}
    {rows.map(criterion => <div key={criterion.id} className="space-y-2 text-sm">
      <p>{criterion.text} · {criterion.verification_profile || 'Profile not defined'} · {(criterion.requirement_links || []).length} requirement link(s)</p>
      {(openId !== criterion.id || !canEdit) && (criterion.requirement_links || []).map(link => <p key={identity(link.requirement_type, link.requirement_id)} className="text-xs text-slate-400">
        {unambiguousOptions.find(item => identity(item.type, item.id) === identity(link.requirement_type, link.requirement_id))?.title || 'Requirement unavailable'} · {link.requirement_id}{link.aspect ? ` · ${link.aspect}` : ''}
      </p>)}
      {canEdit && <button type="button" onClick={() => setOpenId(openId === criterion.id ? null : criterion.id)}>Edit verification {criterion.id}</button>}
      {canEdit && openId === criterion.id && <CriterionEditor key={`${specId}:${version}:${criterion.id}`} specId={specId} version={version} criterion={criterion} options={unambiguousOptions} onSaved={onSaved} />}
    </div>)}
    {!rows.length && <p className="text-xs text-slate-400">No current criteria with supported metadata and stable IDs. Legacy criteria keep their history; use the existing criterion editor to materialize their IDs.</p>}
  </section>;
}
