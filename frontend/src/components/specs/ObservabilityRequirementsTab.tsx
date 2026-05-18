import { useMemo, useState } from 'react';
import { CheckCircle, ChevronDown, ChevronUp, Gauge, Link, Pencil, Plus, Trash2, Unlink, XCircle } from 'lucide-react';
import type { CardSummaryForSpec, ObservabilityRequirement, ObservabilitySignalType, Spec } from '@/types';

interface ObservabilityRequirementsTabProps {
  spec: Spec;
  onUpdate: (requirements: ObservabilityRequirement[]) => void;
  onSpecUpdate?: (patch: Record<string, unknown>) => Promise<void>;
  specCards?: CardSummaryForSpec[];
  onLinkTask?: (requirementId: string, cardId: string) => Promise<void>;
  onUnlinkTask?: (requirementId: string, cardId: string) => Promise<void>;
  canCreate?: boolean;
  canEdit?: boolean;
  canDelete?: boolean;
  canLinkTask?: boolean;
  canEditCoverageFlags?: boolean;
}

const SIGNAL_TYPES: ObservabilitySignalType[] = ['metric', 'log', 'trace', 'dashboard', 'alert', 'slo', 'other'];

const SIGNAL_LABELS: Record<ObservabilitySignalType, string> = {
  metric: 'Metric',
  log: 'Log',
  trace: 'Trace',
  dashboard: 'Dashboard',
  alert: 'Alert',
  slo: 'SLO',
  other: 'Other',
};

function LinkTaskPicker({
  requirementId,
  linkedIds,
  cards,
  onLink,
}: {
  requirementId: string;
  linkedIds: string[];
  cards: CardSummaryForSpec[];
  onLink: (requirementId: string, cardId: string) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const available = cards.filter((card) => !linkedIds.includes(card.id));
  if (available.length === 0) return null;
  return (
    <div className="mt-1">
      <button onClick={() => setOpen(!open)} className="text-[10px] text-blue-500 hover:text-blue-600 dark:text-blue-400">
        {open ? 'Cancel' : '+ Link task'}
      </button>
      {open && (
        <div className="mt-1 border border-gray-200 dark:border-gray-700 rounded p-1.5 max-h-32 overflow-y-auto space-y-0.5">
          {available.map((card) => (
            <button
              key={card.id}
              onClick={async () => { await onLink(requirementId, card.id); setOpen(false); }}
              className="w-full text-left px-2 py-1 rounded text-[11px] text-gray-600 dark:text-gray-400 hover:bg-blue-50 dark:hover:bg-blue-900/20 truncate flex items-center gap-1"
            >
              <Link size={9} className="shrink-0 text-gray-400" />
              {card.title}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export function ObservabilityRequirementsTab({
  spec,
  onUpdate,
  onSpecUpdate,
  specCards = [],
  onLinkTask,
  onUnlinkTask,
  canCreate = true,
  canEdit = true,
  canDelete = true,
  canLinkTask = true,
  canEditCoverageFlags = true,
}: ObservabilityRequirementsTabProps) {
  const [adding, setAdding] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [formTitle, setFormTitle] = useState('');
  const [formSignalType, setFormSignalType] = useState<ObservabilitySignalType>('metric');
  const [formDescription, setFormDescription] = useState('');
  const [formTarget, setFormTarget] = useState('');
  const [formMetricName, setFormMetricName] = useState('');
  const [formThreshold, setFormThreshold] = useState('');
  const [formSeverity, setFormSeverity] = useState('');
  const [formOwner, setFormOwner] = useState('');
  const [formLinkedFRs, setFormLinkedFRs] = useState<string[]>([]);
  const [formLinkedIRs, setFormLinkedIRs] = useState<string[]>([]);
  const [formNotes, setFormNotes] = useState('');

  const requirements = spec.observability_requirements || [];
  const activeRequirements = requirements.filter((item) => item.status === 'active');
  const frs = spec.functional_requirements || [];
  const integrationRequirements = spec.integration_requirements || [];

  const coverage = useMemo(() => {
    const linked = activeRequirements.filter((item) => (item.linked_task_ids?.length ?? 0) > 0).length;
    return {
      linked,
      total: activeRequirements.length,
      pct: activeRequirements.length > 0 ? Math.round((linked / activeRequirements.length) * 100) : 0,
    };
  }, [activeRequirements]);

  const resetForm = () => {
    setFormTitle('');
    setFormSignalType('metric');
    setFormDescription('');
    setFormTarget('');
    setFormMetricName('');
    setFormThreshold('');
    setFormSeverity('');
    setFormOwner('');
    setFormLinkedFRs([]);
    setFormLinkedIRs([]);
    setFormNotes('');
  };

  const buildRequirement = (id: string, current?: ObservabilityRequirement): ObservabilityRequirement => ({
    id,
    title: formTitle.trim(),
    signal_type: formSignalType,
    description: formDescription.trim(),
    target: formTarget.trim() || null,
    metric_name: formMetricName.trim() || null,
    threshold: formThreshold.trim() || null,
    severity: formSeverity.trim() || null,
    owner: formOwner.trim() || null,
    linked_requirements: formLinkedFRs.length > 0 ? formLinkedFRs : null,
    linked_integration_requirements: formLinkedIRs.length > 0 ? formLinkedIRs : null,
    linked_task_ids: current?.linked_task_ids || null,
    status: current?.status || 'active',
    notes: formNotes.trim() || null,
  });

  const handleAdd = () => {
    if (!formTitle.trim() || !formDescription.trim()) return;
    onUpdate([...requirements, buildRequirement(`or_${Date.now()}`)]);
    setAdding(false);
    resetForm();
  };

  const handleEdit = (item: ObservabilityRequirement) => {
    setEditingId(item.id);
    setFormTitle(item.title);
    setFormSignalType(item.signal_type || 'metric');
    setFormDescription(item.description || '');
    setFormTarget(item.target || '');
    setFormMetricName(item.metric_name || '');
    setFormThreshold(item.threshold || '');
    setFormSeverity(item.severity || '');
    setFormOwner(item.owner || '');
    setFormLinkedFRs(item.linked_requirements || []);
    setFormLinkedIRs(item.linked_integration_requirements || []);
    setFormNotes(item.notes || '');
  };

  const handleSaveEdit = () => {
    if (!editingId || !formTitle.trim() || !formDescription.trim()) return;
    onUpdate(requirements.map((item) => item.id === editingId ? buildRequirement(editingId, item) : item));
    setEditingId(null);
    resetForm();
  };

  const handleRemove = (id: string) => {
    if (!confirm('Remove this observability requirement?')) return;
    onUpdate(requirements.filter((item) => item.id !== id));
  };

  const toggleFR = (frIndex: string) => {
    setFormLinkedFRs((current) => current.includes(frIndex) ? current.filter((item) => item !== frIndex) : [...current, frIndex]);
  };

  const toggleIR = (irId: string) => {
    setFormLinkedIRs((current) => current.includes(irId) ? current.filter((item) => item !== irId) : [...current, irId]);
  };

  const isFormValid = Boolean(formTitle.trim() && formDescription.trim());

  const renderForm = (onSubmit: () => void, submitLabel: string, onCancel: () => void) => (
    <div className="border border-emerald-200 dark:border-emerald-700 rounded-lg p-3 space-y-2 bg-emerald-50/50 dark:bg-emerald-900/10">
      <div className="grid grid-cols-[150px_minmax(0,1fr)] gap-2">
        <select value={formSignalType} onChange={(event) => setFormSignalType(event.target.value as ObservabilitySignalType)} className="px-2 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600">
          {SIGNAL_TYPES.map((type) => <option key={type} value={type}>{SIGNAL_LABELS[type]}</option>)}
        </select>
        <input value={formTitle} onChange={(event) => setFormTitle(event.target.value)} placeholder="Observability requirement title" className="px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600" autoFocus />
      </div>
      <textarea value={formDescription} onChange={(event) => setFormDescription(event.target.value)} placeholder="Dashboard, metric, alert, log, trace, or SLO expectation" className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600 resize-none" rows={2} />
      <div className="grid grid-cols-2 gap-2">
        <input value={formTarget} onChange={(event) => setFormTarget(event.target.value)} placeholder="Target component or flow" className="px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600" />
        <input value={formMetricName} onChange={(event) => setFormMetricName(event.target.value)} placeholder="Metric/query/dashboard" className="px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600" />
        <input value={formThreshold} onChange={(event) => setFormThreshold(event.target.value)} placeholder="Threshold or SLO" className="px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600" />
        <input value={formSeverity} onChange={(event) => setFormSeverity(event.target.value)} placeholder="Severity" className="px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600" />
      </div>
      <input value={formOwner} onChange={(event) => setFormOwner(event.target.value)} placeholder="Owner" className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600" />
      <textarea value={formNotes} onChange={(event) => setFormNotes(event.target.value)} placeholder="Notes (optional)" className="w-full px-2 py-1.5 border border-gray-300 rounded-lg text-xs dark:bg-gray-700 dark:border-gray-600 resize-none" rows={1} />

      {frs.length > 0 && (
        <div>
          <span className="text-[10px] text-gray-500 dark:text-gray-400 block mb-1">Link to functional requirements:</span>
          <div className="flex flex-wrap gap-1">
            {frs.map((fr, index) => {
              const key = String(index);
              const linked = formLinkedFRs.includes(key);
              return (
                <button key={key} onClick={() => toggleFR(key)} className={`text-[10px] px-1.5 py-0.5 rounded transition-colors ${linked ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300 ring-1 ring-emerald-400' : 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400 hover:bg-gray-200'}`}>
                  FR{index}: {fr.length > 42 ? fr.slice(0, 39) + '...' : fr}
                </button>
              );
            })}
          </div>
        </div>
      )}

      {integrationRequirements.length > 0 && (
        <div>
          <span className="text-[10px] text-gray-500 dark:text-gray-400 block mb-1">Link to integration requirements:</span>
          <div className="flex flex-wrap gap-1">
            {integrationRequirements.map((item) => {
              const linked = formLinkedIRs.includes(item.id);
              return (
                <button key={item.id} onClick={() => toggleIR(item.id)} className={`text-[10px] px-1.5 py-0.5 rounded transition-colors ${linked ? 'bg-sky-100 text-sky-700 dark:bg-sky-900/40 dark:text-sky-300 ring-1 ring-sky-400' : 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400 hover:bg-gray-200'}`}>
                  {item.title.length > 42 ? item.title.slice(0, 39) + '...' : item.title}
                </button>
              );
            })}
          </div>
        </div>
      )}

      <div className="flex justify-end gap-2">
        <button onClick={onCancel} className="btn btn-secondary text-xs">Cancel</button>
        <button onClick={onSubmit} disabled={!isFormValid} className="btn btn-primary text-xs">{submitLabel}</button>
      </div>
    </div>
  );

  return (
    <div className="space-y-4">
      {activeRequirements.length > 0 && (
        <div className="border border-gray-200 dark:border-gray-700 rounded-lg p-3">
          <div className="flex items-center justify-between mb-2">
            <h4 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide">OR Task Coverage ({coverage.linked}/{coverage.total})</h4>
            <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${coverage.linked === coverage.total ? 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300' : 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300'}`}>
              {coverage.pct}% linked
            </span>
          </div>
          <div className="h-2 bg-gray-100 dark:bg-gray-700 rounded-full overflow-hidden">
            <div className={`h-full transition-all duration-500 rounded-full ${coverage.linked === coverage.total ? 'bg-green-500' : 'bg-amber-500'}`} style={{ width: `${coverage.pct}%` }} />
          </div>
        </div>
      )}

      {onSpecUpdate && canEditCoverageFlags && (
        <div className="flex items-center justify-between px-3 py-2 rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50/50 dark:bg-gray-700/20">
          <div>
            <span className="text-xs font-medium text-gray-700 dark:text-gray-300">Skip OR coverage requirement</span>
            <p className="text-[10px] text-gray-400">Allow validation without full OR-to-task coverage</p>
          </div>
          <button onClick={() => onSpecUpdate({ skip_or_coverage: !spec.skip_or_coverage })} className={`relative w-10 h-5 rounded-full transition-colors ${spec.skip_or_coverage ? 'bg-amber-500' : 'bg-gray-300 dark:bg-gray-600'}`}>
            <span className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white transition-transform ${spec.skip_or_coverage ? 'translate-x-5' : ''}`} />
          </button>
        </div>
      )}

      {requirements.length === 0 && !adding && (
        <div className="text-center py-6">
          <Gauge size={32} className="mx-auto text-gray-300 dark:text-gray-600 mb-2" />
          <p className="text-sm text-gray-500 dark:text-gray-400">No observability requirements defined</p>
        </div>
      )}

      {requirements.map((item) => {
        const expanded = expandedId === item.id;
        const editing = editingId === item.id;
        const taskCount = item.linked_task_ids?.length ?? 0;
        if (editing) {
          return <div key={item.id}>{renderForm(handleSaveEdit, 'Save', () => { setEditingId(null); resetForm(); })}</div>;
        }
        return (
          <div key={item.id} className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
            <div className="flex items-center gap-2 px-3 py-2 cursor-pointer bg-gray-50 dark:bg-gray-700/50" onClick={() => setExpandedId(expanded ? null : item.id)}>
              {taskCount > 0 ? <CheckCircle size={14} className="text-green-500 shrink-0" /> : <XCircle size={14} className="text-gray-300 dark:text-gray-600 shrink-0" />}
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300 font-medium">{SIGNAL_LABELS[item.signal_type]}</span>
              <span className="text-sm font-medium text-gray-900 dark:text-white truncate flex-1">{item.title}</span>
              <span className={`text-[10px] px-1.5 py-0.5 rounded ${taskCount > 0 ? 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300' : 'bg-gray-100 text-gray-400 dark:bg-gray-700 dark:text-gray-500'}`}>{taskCount} tasks</span>
              {canEdit && (
                <button onClick={(event) => { event.stopPropagation(); handleEdit(item); }} className="p-0.5 text-gray-400 hover:text-blue-500"><Pencil size={12} /></button>
              )}
              {canDelete && (
                <button onClick={(event) => { event.stopPropagation(); handleRemove(item.id); }} className="p-0.5 text-gray-400 hover:text-red-500"><Trash2 size={12} /></button>
              )}
              {expanded ? <ChevronUp size={14} className="text-gray-400" /> : <ChevronDown size={14} className="text-gray-400" />}
            </div>
            {expanded && (
              <div className="px-3 py-2 space-y-2 text-sm">
                <p className="text-xs text-gray-600 dark:text-gray-400">{item.description}</p>
                <div className="grid grid-cols-2 gap-2 text-xs text-gray-500 dark:text-gray-400">
                  {item.target && <span>Target: {item.target}</span>}
                  {item.metric_name && <span>Metric: {item.metric_name}</span>}
                  {item.threshold && <span>Threshold: {item.threshold}</span>}
                  {item.severity && <span>Severity: {item.severity}</span>}
                  {item.owner && <span>Owner: {item.owner}</span>}
                </div>
                {item.linked_integration_requirements && item.linked_integration_requirements.length > 0 && (
                  <div className="flex flex-wrap gap-1">
                    <span className="text-[10px] text-gray-400 mr-1">IRs:</span>
                    {item.linked_integration_requirements.map((id) => <span key={id} className="text-[10px] px-1.5 py-0.5 rounded bg-sky-50 text-sky-700 dark:bg-sky-900/20 dark:text-sky-300">{id}</span>)}
                  </div>
                )}
                {item.notes && <p className="text-xs text-gray-500 dark:text-gray-400 italic border-l-2 border-gray-300 dark:border-gray-600 pl-2">{item.notes}</p>}
                {item.linked_task_ids && item.linked_task_ids.length > 0 && (
                  <div className="flex flex-wrap gap-1">
                    <span className="text-[10px] text-gray-400 mr-1">Linked Tasks:</span>
                    {item.linked_task_ids.map((taskId) => (
                      canLinkTask && onUnlinkTask ? (
                        <button key={taskId} onClick={() => onUnlinkTask(item.id, taskId)} className="text-[10px] px-1.5 py-0.5 rounded bg-green-50 text-green-700 dark:bg-green-900/20 dark:text-green-300 inline-flex items-center gap-1">
                          {taskId.slice(0, 8)} <Unlink size={9} />
                        </button>
                      ) : (
                        <span key={taskId} className="text-[10px] px-1.5 py-0.5 rounded bg-green-50 text-green-700 dark:bg-green-900/20 dark:text-green-300">
                          {taskId.slice(0, 8)}
                        </span>
                      )
                    ))}
                  </div>
                )}
                {canLinkTask && onLinkTask && <LinkTaskPicker requirementId={item.id} linkedIds={item.linked_task_ids || []} cards={specCards} onLink={onLinkTask} />}
              </div>
            )}
          </div>
        );
      })}

      {adding ? (
        renderForm(handleAdd, 'Add OR', () => { setAdding(false); resetForm(); })
      ) : canCreate ? (
        <button onClick={() => setAdding(true)} className="w-full py-2 border-2 border-dashed border-gray-300 dark:border-gray-600 rounded-lg text-sm text-gray-500 hover:border-emerald-400 hover:text-emerald-500 transition-colors flex items-center justify-center gap-1">
          <Plus size={14} />
          Add Observability Requirement
        </button>
      ) : null}
    </div>
  );
}
