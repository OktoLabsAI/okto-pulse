import { useEffect, useMemo, useState } from 'react';
import { CheckCircle, ChevronDown, ChevronUp, Link, Network, Pencil, Plus, Trash2, Unlink, XCircle } from 'lucide-react';
import type { ApiContract, CardSummaryForSpec, IntegrationRequirement, IntegrationRequirementType, Spec } from '@/types';

interface IntegrationRequirementsTabProps {
  spec: Spec;
  onUpdate: (requirements: IntegrationRequirement[]) => void;
  onSpecUpdate?: (patch: Record<string, unknown>) => Promise<void>;
  specCards?: CardSummaryForSpec[];
  onLinkTask?: (requirementId: string, cardId: string) => Promise<void>;
  onUnlinkTask?: (requirementId: string, cardId: string) => Promise<void>;
  canCreate?: boolean;
  canEdit?: boolean;
  canDelete?: boolean;
  canLinkTask?: boolean;
  canEditCoverageFlags?: boolean;
  focusEditId?: string | null;
  focusCreateToken?: number | null;
  onFocusHandled?: () => void;
}

const TYPES: IntegrationRequirementType[] = ['api', 'queue', 'stored_procedure', 'data_contract', 'event', 'file', 'other'];

const TYPE_LABELS: Record<IntegrationRequirementType, string> = {
  api: 'API',
  queue: 'Queue',
  stored_procedure: 'SP',
  data_contract: 'Data',
  event: 'Event',
  file: 'File',
  other: 'Other',
};

function parseJSON(value: string): Record<string, unknown> | null {
  if (!value.trim()) return null;
  try {
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed as Record<string, unknown> : null;
  } catch {
    return null;
  }
}

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

export function IntegrationRequirementsTab({
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
  focusEditId = null,
  focusCreateToken = null,
  onFocusHandled,
}: IntegrationRequirementsTabProps) {
  const [adding, setAdding] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [formTitle, setFormTitle] = useState('');
  const [formType, setFormType] = useState<IntegrationRequirementType>('api');
  const [formDescription, setFormDescription] = useState('');
  const [formProvider, setFormProvider] = useState('');
  const [formConsumer, setFormConsumer] = useState('');
  const [formContractRef, setFormContractRef] = useState('');
  const [formEndpoint, setFormEndpoint] = useState('');
  const [formMethod, setFormMethod] = useState('');
  const [formDataContract, setFormDataContract] = useState('');
  const [formLinkedFRs, setFormLinkedFRs] = useState<string[]>([]);
  const [formLinkedContracts, setFormLinkedContracts] = useState<string[]>([]);
  const [formNotes, setFormNotes] = useState('');

  const requirements = (spec.integration_requirements || []).filter((item) => item.status === 'active');
  const activeRequirements = requirements.filter((item) => item.status === 'active');
  const frs = (spec.functional_requirements || []).map((fr: any) =>
    typeof fr === 'string' ? fr : String(fr?.text || fr?.title || '')
  );
  const contracts = spec.api_contracts || [];

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
    setFormType('api');
    setFormDescription('');
    setFormProvider('');
    setFormConsumer('');
    setFormContractRef('');
    setFormEndpoint('');
    setFormMethod('');
    setFormDataContract('');
    setFormLinkedFRs([]);
    setFormLinkedContracts([]);
    setFormNotes('');
  };

  const buildRequirement = (id: string, current?: IntegrationRequirement): IntegrationRequirement => ({
    id,
    title: formTitle.trim(),
    integration_type: formType,
    description: formDescription.trim(),
    provider: formProvider.trim() || null,
    consumer: formConsumer.trim() || null,
    contract_ref: formContractRef.trim() || null,
    endpoint: formEndpoint.trim() || null,
    method: formMethod.trim() || null,
    data_contract: parseJSON(formDataContract),
    linked_requirements: formLinkedFRs.length > 0 ? formLinkedFRs : null,
    linked_api_contracts: formLinkedContracts.length > 0 ? formLinkedContracts : null,
    linked_task_ids: current?.linked_task_ids || null,
    status: current?.status || 'active',
    notes: formNotes.trim() || null,
  });

  const handleAdd = () => {
    if (!formTitle.trim() || !formDescription.trim()) return;
    onUpdate([...requirements, buildRequirement(`ir_${Date.now()}`)]);
    setAdding(false);
    resetForm();
  };

  const handleEdit = (item: IntegrationRequirement) => {
    setEditingId(item.id);
    setFormTitle(item.title);
    setFormType(item.integration_type || 'api');
    setFormDescription(item.description || '');
    setFormProvider(item.provider || '');
    setFormConsumer(item.consumer || '');
    setFormContractRef(item.contract_ref || '');
    setFormEndpoint(item.endpoint || '');
    setFormMethod(item.method || '');
    setFormDataContract(item.data_contract ? JSON.stringify(item.data_contract, null, 2) : '');
    setFormLinkedFRs(item.linked_requirements || []);
    setFormLinkedContracts(item.linked_api_contracts || []);
    setFormNotes(item.notes || '');
  };

  useEffect(() => {
    if (!focusEditId || !canEdit) return;
    const target = requirements.find((item) => item.id === focusEditId);
    if (!target) return;
    handleEdit(target);
    setExpandedId(null);
    onFocusHandled?.();
  }, [focusEditId, canEdit, spec.integration_requirements, onFocusHandled]);

  useEffect(() => {
    if (!focusCreateToken || !canCreate) return;
    resetForm();
    setAdding(true);
    setEditingId(null);
    onFocusHandled?.();
  }, [focusCreateToken, canCreate, onFocusHandled]);

  const handleSaveEdit = () => {
    if (!editingId || !formTitle.trim() || !formDescription.trim()) return;
    onUpdate(requirements.map((item) => item.id === editingId ? buildRequirement(editingId, item) : item));
    setEditingId(null);
    resetForm();
  };

  const handleRemove = (id: string) => {
    if (!confirm('Remove this integration requirement?')) return;
    onUpdate(requirements.filter((item) => item.id !== id));
  };

  const toggleFR = (frIndex: string) => {
    setFormLinkedFRs((current) => current.includes(frIndex) ? current.filter((item) => item !== frIndex) : [...current, frIndex]);
  };

  const toggleContract = (contractId: string) => {
    setFormLinkedContracts((current) => current.includes(contractId) ? current.filter((item) => item !== contractId) : [...current, contractId]);
  };

  const isFormValid = Boolean(formTitle.trim() && formDescription.trim());

  const renderForm = (onSubmit: () => void, submitLabel: string, onCancel: () => void) => (
    <div className="border border-sky-200 dark:border-sky-700 rounded-lg p-3 space-y-2 bg-sky-50/50 dark:bg-sky-900/10">
      <div className="grid grid-cols-[150px_minmax(0,1fr)] gap-2">
        <select value={formType} onChange={(event) => setFormType(event.target.value as IntegrationRequirementType)} className="px-2 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600">
          {TYPES.map((type) => <option key={type} value={type}>{TYPE_LABELS[type]}</option>)}
        </select>
        <input value={formTitle} onChange={(event) => setFormTitle(event.target.value)} placeholder="Integration requirement title" className="px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600" autoFocus />
      </div>
      <textarea value={formDescription} onChange={(event) => setFormDescription(event.target.value)} placeholder="Contract, queue, API, stored procedure, event, or file integration expectation" className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600 resize-none" rows={2} />
      <div className="grid grid-cols-2 gap-2">
        <input value={formProvider} onChange={(event) => setFormProvider(event.target.value)} placeholder="Provider" className="px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600" />
        <input value={formConsumer} onChange={(event) => setFormConsumer(event.target.value)} placeholder="Consumer" className="px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600" />
        <input value={formEndpoint} onChange={(event) => setFormEndpoint(event.target.value)} placeholder="Endpoint, topic, procedure, or file" className="px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600" />
        <input value={formMethod} onChange={(event) => setFormMethod(event.target.value)} placeholder="Method/action" className="px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600" />
      </div>
      <input value={formContractRef} onChange={(event) => setFormContractRef(event.target.value)} placeholder="Contract reference" className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600" />
      <textarea value={formDataContract} onChange={(event) => setFormDataContract(event.target.value)} placeholder='Data contract JSON, e.g. {"event":"metric.created"}' className="w-full px-2 py-1.5 border border-gray-300 rounded-lg text-xs font-mono dark:bg-gray-700 dark:border-gray-600 resize-none" rows={3} />
      <textarea value={formNotes} onChange={(event) => setFormNotes(event.target.value)} placeholder="Notes (optional)" className="w-full px-2 py-1.5 border border-gray-300 rounded-lg text-xs dark:bg-gray-700 dark:border-gray-600 resize-none" rows={1} />

      {frs.length > 0 && (
        <div>
          <span className="text-[10px] text-gray-500 dark:text-gray-400 block mb-1">Link to functional requirements:</span>
          <div className="flex flex-wrap gap-1">
            {frs.map((fr, index) => {
              const key = String(index);
              const linked = formLinkedFRs.includes(key);
              return (
                <button key={key} onClick={() => toggleFR(key)} className={`text-[10px] px-1.5 py-0.5 rounded transition-colors ${linked ? 'bg-sky-100 text-sky-700 dark:bg-sky-900/40 dark:text-sky-300 ring-1 ring-sky-400' : 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400 hover:bg-gray-200'}`}>
                  FR{index}: {fr.length > 42 ? fr.slice(0, 39) + '...' : fr}
                </button>
              );
            })}
          </div>
        </div>
      )}

      {contracts.length > 0 && (
        <div>
          <span className="text-[10px] text-gray-500 dark:text-gray-400 block mb-1">Link to API contracts:</span>
          <div className="flex flex-wrap gap-1">
            {contracts.map((contract: ApiContract) => {
              const linked = formLinkedContracts.includes(contract.id);
              return (
                <button key={contract.id} onClick={() => toggleContract(contract.id)} className={`text-[10px] px-1.5 py-0.5 rounded transition-colors ${linked ? 'bg-cyan-100 text-cyan-700 dark:bg-cyan-900/40 dark:text-cyan-300 ring-1 ring-cyan-400' : 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400 hover:bg-gray-200'}`}>
                  {contract.method} {contract.path}
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
            <h4 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide">IR Task Coverage ({coverage.linked}/{coverage.total})</h4>
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
            <span className="text-xs font-medium text-gray-700 dark:text-gray-300">Skip IR coverage requirement</span>
            <p className="text-[10px] text-gray-400">Allow validation without full IR-to-task coverage</p>
          </div>
          <button onClick={() => onSpecUpdate({ skip_ir_coverage: !spec.skip_ir_coverage })} className={`relative w-10 h-5 rounded-full transition-colors ${spec.skip_ir_coverage ? 'bg-amber-500' : 'bg-gray-300 dark:bg-gray-600'}`}>
            <span className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white transition-transform ${spec.skip_ir_coverage ? 'translate-x-5' : ''}`} />
          </button>
        </div>
      )}

      {requirements.length === 0 && !adding && (
        <div className="text-center py-6">
          <Network size={32} className="mx-auto text-gray-300 dark:text-gray-600 mb-2" />
          <p className="text-sm text-gray-500 dark:text-gray-400">No integration requirements defined</p>
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
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-sky-100 text-sky-700 dark:bg-sky-900/40 dark:text-sky-300 font-medium">{TYPE_LABELS[item.integration_type]}</span>
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
                  {item.provider && <span>Provider: {item.provider}</span>}
                  {item.consumer && <span>Consumer: {item.consumer}</span>}
                  {item.endpoint && <span className="font-mono">Endpoint: {item.endpoint}</span>}
                  {item.method && <span>Method: {item.method}</span>}
                </div>
                {item.linked_api_contracts && item.linked_api_contracts.length > 0 && (
                  <div className="flex flex-wrap gap-1">
                    <span className="text-[10px] text-gray-400 mr-1">Contracts:</span>
                    {item.linked_api_contracts.map((id) => <span key={id} className="text-[10px] px-1.5 py-0.5 rounded bg-cyan-50 text-cyan-700 dark:bg-cyan-900/20 dark:text-cyan-300">{id}</span>)}
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
        renderForm(handleAdd, 'Add IR', () => { setAdding(false); resetForm(); })
      ) : canCreate ? (
        <button onClick={() => setAdding(true)} className="w-full py-2 border-2 border-dashed border-gray-300 dark:border-gray-600 rounded-lg text-sm text-gray-500 hover:border-sky-400 hover:text-sky-500 transition-colors flex items-center justify-center gap-1">
          <Plus size={14} />
          Add Integration Requirement
        </button>
      ) : null}
    </div>
  );
}
