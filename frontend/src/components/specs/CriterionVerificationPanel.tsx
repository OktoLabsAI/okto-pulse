import { useEffect, useRef, useState } from 'react';
import { Pencil } from 'lucide-react';
import { useDashboardApi } from '@/services/api';
import { verificationButton, verificationCard, verificationInput, requirementTypeLabels } from './verificationPresentation';

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
  title?: string | null;
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
  return <div className={verificationCard}>
    <fieldset disabled={busy || saved} className="space-y-3">
      <label className="block text-sm">Verification profile
        <select aria-label="Verification profile" className={verificationInput} value={profile} onChange={event => setProfile(event.target.value as VerificationProfile | '')}>
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
            <input aria-label={`Covered aspect ${index + 1}`} maxLength={2000} className={verificationInput} value={link.aspect || ''} onChange={event => setLinks(links.map((item, i) => i === index ? { ...item, aspect: event.target.value } : item))} />
          </label>
          <button className={verificationButton} type="button" onClick={() => setLinks(links.filter((_, i) => i !== index))}>Remove link {index + 1}</button>
        </div>;
      })}
      <div className="flex gap-2">
        <select aria-label="Requirement to link" className={verificationInput} value={selection} onChange={event => setSelection(event.target.value)}>
          <option value="">Select a requirement</option>
          {available.map(option => <option key={identity(option.type, option.id)} value={identity(option.type, option.id)}>{option.type}: {option.title} ({option.id})</option>)}
        </select>
        <button className={verificationButton} type="button" disabled={!selection || links.length >= 100} onClick={() => {
          const option = available.find(item => identity(item.type, item.id) === selection);
          if (option) setLinks([...links, { requirement_type: option.type, requirement_id: option.id }]);
          setSelection('');
        }}>Add link</button>
      </div>
      <button className={verificationButton} type="button" onClick={() => void save()}>{busy ? 'Saving…' : 'Save verification plan'}</button>
    </fieldset>
    {error && <p role="alert">{error}</p>}
    {saved && <button className={verificationButton} type="button" onClick={() => void refresh()}>Reload saved criterion</button>}
  </div>;
}

/** Verification is a facet of the expanded AC, never a second criterion list. */
export function CriterionVerificationDetails({ specId, version, value, options, canEdit, onSaved }: {
  specId: string; version: number; value: unknown;
  options: VerificationRequirementOption[]; canEdit: boolean; onSaved: () => Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const criterion = editableCriterion(value);
  const choices = options.filter(option => options.filter(item => identity(item.type, item.id) === identity(option.type, option.id)).length === 1);
  if (!criterion) return <p role="status">Verification metadata unavailable; review this AC before planning execution.</p>;
  return <section aria-label="AC verification" className="space-y-3 border-t border-gray-200 pt-3 dark:border-gray-700">
    <div className="flex items-center justify-between gap-2">
      <h4 className="text-xs font-semibold">Verification</h4>
      {canEdit && <button type="button" className="p-1 text-gray-400 hover:text-blue-500" aria-label={`Edit verification ${criterion.id}`} onClick={() => setEditing(!editing)}><Pencil size={14} /></button>}
    </div>
    {editing && canEdit ? <CriterionEditor key={`${specId}:${version}:${criterion.id}`} specId={specId} version={version} criterion={criterion} options={choices} onSaved={onSaved} /> : <>
      <p>Profile: <span className="font-medium">{criterion.verification_profile || 'Not defined'}</span></p>
      <div className="grid gap-2 sm:grid-cols-2">{(criterion.requirement_links || []).map(link => <div key={identity(link.requirement_type, link.requirement_id)} className="space-y-1 rounded-lg bg-gray-50 p-2 dark:bg-gray-900/40">
        <span className="text-xs font-medium text-blue-600 dark:text-blue-400">{requirementTypeLabels[link.requirement_type]}</span>
        <p className="break-words">{choices.find(item => identity(item.type, item.id) === identity(link.requirement_type, link.requirement_id))?.title || 'Requirement unavailable'} · {link.requirement_id}</p>
        {link.aspect && <p className="whitespace-pre-wrap">Covered aspect: {link.aspect}</p>}
      </div>)}</div>
      {!criterion.requirement_links?.length && <p>No requirements linked.</p>}
    </>}
  </section>;
}
