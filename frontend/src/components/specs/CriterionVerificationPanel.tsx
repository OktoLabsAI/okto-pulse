import { useEffect, useRef, useState } from 'react';
import { ChevronDown, ChevronUp, Pencil } from 'lucide-react';
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

export function CriterionVerificationPanel({ specId, version, criteria, options, canEdit, onSaved }: {
  specId: string; version: number; criteria: unknown[];
  options: VerificationRequirementOption[]; canEdit: boolean; onSaved: () => Promise<void>;
}) {
  const [openId, setOpenId] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const parsed = criteria.map(editableCriterion).filter((row): row is Criterion => row !== null);
  const rows = parsed.filter(row => parsed.filter(item => item.id === row.id).length === 1);
  const currentCount = criteria.filter(value => !value || typeof value !== 'object' || !('status' in value) || value.status == null || value.status === 'active').length;
  const unambiguousOptions = options.filter(option => options.filter(item => identity(item.type, item.id) === identity(option.type, option.id)).length === 1);
  return <section aria-label="Criterion verification" className="space-y-3">
    <h4 className="text-sm font-medium">Criterion verification</h4>
    <p className="text-xs text-slate-400">Define each criterion’s profile and requirement links. Planning completeness and evidence are evaluated separately.</p>
    {currentCount > rows.length && <p role="status">{currentCount - rows.length} criterion(s) have legacy or unsupported identity/metadata and require review.</p>}
    {rows.map((criterion, index) => <article key={criterion.id} className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden" aria-label={criterion.title || `Criterion ${index + 1}`}>
      <header className="flex items-center gap-2 px-3 py-2 bg-gray-50 dark:bg-gray-700/50">
        <button type="button" aria-expanded={expandedId === criterion.id} aria-controls={`criterion-details-${criterion.id}`} onClick={() => { setExpandedId(expandedId === criterion.id ? null : criterion.id); setOpenId(null); }} className="flex min-w-0 flex-1 items-center gap-2 text-left">
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-sky-100 text-sky-700 dark:bg-sky-900/40 dark:text-sky-300 font-medium">AC {index + 1}</span>
          <span className="text-sm font-medium text-gray-900 dark:text-white truncate flex-1" title={criterion.title || criterion.text}>{criterion.title || criterion.text}</span>
          <span className="shrink-0 text-[10px] px-1.5 py-0.5 rounded bg-blue-50 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300">{criterion.verification_profile || 'Profile not defined'}</span>
          <span className="shrink-0 text-[10px] text-gray-500">{(criterion.requirement_links || []).length} links</span>
          {expandedId === criterion.id ? <ChevronUp size={14} className="shrink-0 text-gray-400" /> : <ChevronDown size={14} className="shrink-0 text-gray-400" />}
        </button>
        {canEdit && <button type="button" className="p-0.5 text-gray-400 hover:text-blue-500" aria-label={`Edit verification ${criterion.id}`} onClick={() => { setExpandedId(criterion.id); setOpenId(openId === criterion.id ? null : criterion.id); }}><Pencil size={12} /></button>}
      </header>
      {expandedId === criterion.id && <div id={`criterion-details-${criterion.id}`} className="px-3 py-2 space-y-2 text-xs text-gray-600 dark:text-gray-400">
      <p className="whitespace-pre-wrap break-words">{criterion.text}</p>
      <p className="font-medium">{(criterion.requirement_links || []).length} requirement link(s)</p>
      {(openId !== criterion.id || !canEdit) && <div className="grid gap-2 sm:grid-cols-2">{(criterion.requirement_links || []).map(link => <div key={identity(link.requirement_type, link.requirement_id)} className="space-y-2 rounded-lg bg-gray-50 p-3 dark:bg-gray-900/40">
        <span className="text-xs font-medium text-blue-600 dark:text-blue-400">{requirementTypeLabels[link.requirement_type]}</span>
        <p className="break-words text-sm">{unambiguousOptions.find(item => identity(item.type, item.id) === identity(link.requirement_type, link.requirement_id))?.title || 'Requirement unavailable'} · {link.requirement_id}</p>
        {link.aspect && <dl className="text-xs"><dt className="font-medium text-gray-500">Covered aspect</dt><dd className="mt-1 whitespace-pre-wrap">{link.aspect}</dd></dl>}
      </div>)}</div>}
      {canEdit && openId === criterion.id && <CriterionEditor key={`${specId}:${version}:${criterion.id}`} specId={specId} version={version} criterion={criterion} options={unambiguousOptions} onSaved={onSaved} />}
      <details className="text-xs text-gray-500"><summary className="cursor-pointer">Criterion reference</summary><code className="block break-all pt-1">{criterion.id}</code></details>
      </div>}
    </article>)}
    {!rows.length && <p className="text-xs text-slate-400">No current criteria with supported metadata and stable IDs. Legacy criteria keep their history; use the existing criterion editor to materialize their IDs.</p>}
  </section>;
}
