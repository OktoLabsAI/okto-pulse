/**
 * ContractsTab - API contracts management for specs
 */

import { useState } from 'react';
import { Plus, Trash2, ChevronDown, ChevronUp, FileCode, Pencil, Link, Unlink } from 'lucide-react';
import type { Spec, ApiContract, CardSummaryForSpec } from '@/types';

interface ContractsTabProps {
  spec: Spec;
  onUpdate: (contracts: ApiContract[]) => void;
  onSpecUpdate?: (data: Record<string, unknown>) => Promise<void>;
  specCards?: CardSummaryForSpec[];
  onLinkTask?: (contractId: string, cardId: string) => Promise<void>;
  onUnlinkTask?: (contractId: string, cardId: string) => Promise<void>;
}

const METHOD_COLORS: Record<string, string> = {
  GET: 'bg-green-500 text-white',
  POST: 'bg-blue-500 text-white',
  PUT: 'bg-amber-500 text-white',
  DELETE: 'bg-red-500 text-white',
  PATCH: 'bg-cyan-500 text-white',
  TOOL: 'bg-violet-500 text-white',
  COMPONENT: 'bg-teal-500 text-white',
  EVENT: 'bg-pink-500 text-white',
};

const ALL_METHODS = ['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'TOOL', 'COMPONENT', 'EVENT'];

function tryParseJSON(str: string): Record<string, unknown> | null {
  if (!str.trim()) return null;
  try {
    return JSON.parse(str);
  } catch {
    return null;
  }
}

function tryParseJSONArray(str: string): Array<Record<string, unknown>> | null {
  if (!str.trim()) return null;
  try {
    const parsed = JSON.parse(str);
    return Array.isArray(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function LinkTaskPicker({ contractId, linkedIds, cards, onLink }: {
  contractId: string;
  linkedIds: string[];
  cards: CardSummaryForSpec[];
  onLink: (contractId: string, cardId: string) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const available = cards.filter((c) => !linkedIds.includes(c.id));
  if (available.length === 0) return null;
  return (
    <div className="mt-1">
      <button onClick={() => setOpen(!open)} className="text-[10px] text-blue-500 hover:text-blue-600 dark:text-blue-400">
        {open ? 'Cancel' : '+ Link task'}
      </button>
      {open && (
        <div className="mt-1 border border-gray-200 dark:border-gray-700 rounded p-1.5 max-h-32 overflow-y-auto space-y-0.5">
          {available.map((c) => (
            <button
              key={c.id}
              onClick={async () => { await onLink(contractId, c.id); setOpen(false); }}
              className="w-full text-left px-2 py-1 rounded text-[11px] text-gray-600 dark:text-gray-400 hover:bg-blue-50 dark:hover:bg-blue-900/20 truncate flex items-center gap-1"
            >
              <Link size={9} className="shrink-0 text-gray-400" />
              {c.title}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export function ContractsTab({ spec, onUpdate, onSpecUpdate, specCards, onLinkTask, onUnlinkTask }: ContractsTabProps) {
  const [adding, setAdding] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  // Form state
  const [formMethod, setFormMethod] = useState('GET');
  const [formPath, setFormPath] = useState('');
  const [formDescription, setFormDescription] = useState('');
  const [formRequestBody, setFormRequestBody] = useState('');
  const [formResponseSuccess, setFormResponseSuccess] = useState('');
  const [formResponseErrors, setFormResponseErrors] = useState('');
  const [formLinkedFRs, setFormLinkedFRs] = useState<string[]>([]);
  const [formLinkedBRs, setFormLinkedBRs] = useState<string[]>([]);
  const [formNotes, setFormNotes] = useState('');

  const contracts = spec.api_contracts || [];
  const frs = spec.functional_requirements || [];
  const brs = spec.business_rules || [];

  const resetForm = () => {
    setFormMethod('GET');
    setFormPath('');
    setFormDescription('');
    setFormRequestBody('');
    setFormResponseSuccess('');
    setFormResponseErrors('');
    setFormLinkedFRs([]);
    setFormLinkedBRs([]);
    setFormNotes('');
  };

  const buildContract = (id: string): ApiContract => ({
    id,
    method: formMethod,
    path: formPath.trim(),
    description: formDescription.trim(),
    request_body: tryParseJSON(formRequestBody),
    response_success: tryParseJSON(formResponseSuccess),
    response_errors: tryParseJSONArray(formResponseErrors),
    linked_requirements: formLinkedFRs.length > 0 ? formLinkedFRs : null,
    linked_rules: formLinkedBRs.length > 0 ? formLinkedBRs : null,
    linked_task_ids: null,
    notes: formNotes.trim() || null,
  });

  const handleAdd = () => {
    if (!formPath.trim() || !formDescription.trim()) return;
    const id = `ac_${Date.now()}`;
    onUpdate([...contracts, buildContract(id)]);
    setAdding(false);
    resetForm();
  };

  const handleEdit = (contract: ApiContract) => {
    setEditingId(contract.id);
    setFormMethod(contract.method);
    setFormPath(contract.path);
    setFormDescription(contract.description);
    setFormRequestBody(contract.request_body ? JSON.stringify(contract.request_body, null, 2) : '');
    setFormResponseSuccess(contract.response_success ? JSON.stringify(contract.response_success, null, 2) : '');
    setFormResponseErrors(contract.response_errors ? JSON.stringify(contract.response_errors, null, 2) : '');
    setFormLinkedFRs(contract.linked_requirements || []);
    setFormLinkedBRs(contract.linked_rules || []);
    setFormNotes(contract.notes || '');
  };

  const handleSaveEdit = () => {
    if (!editingId || !formPath.trim() || !formDescription.trim()) return;
    onUpdate(contracts.map((c) => c.id === editingId ? buildContract(editingId) : c));
    setEditingId(null);
    resetForm();
  };

  const handleRemove = (id: string) => {
    if (!confirm('Remove this API contract?')) return;
    onUpdate(contracts.filter((c) => c.id !== id));
  };

  const toggleFR = (fr: string) => {
    setFormLinkedFRs((prev) =>
      prev.includes(fr) ? prev.filter((x) => x !== fr) : [...prev, fr]
    );
  };

  const toggleBR = (brId: string) => {
    setFormLinkedBRs((prev) =>
      prev.includes(brId) ? prev.filter((x) => x !== brId) : [...prev, brId]
    );
  };

  const isFormValid = formPath.trim() && formDescription.trim();

  const renderForm = (onSubmit: () => void, submitLabel: string, onCancel: () => void) => (
    <div className="border border-cyan-200 dark:border-cyan-700 rounded-lg p-3 space-y-2 bg-cyan-50/50 dark:bg-cyan-900/10">
      <div className="flex gap-2">
        <select
          value={formMethod}
          onChange={(e) => setFormMethod(e.target.value)}
          className="px-2 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600 font-mono font-bold"
        >
          {ALL_METHODS.map((m) => <option key={m} value={m}>{m}</option>)}
        </select>
        <input
          type="text"
          value={formPath}
          onChange={(e) => setFormPath(e.target.value)}
          placeholder="/api/v1/resource or component name"
          className="flex-1 px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600 font-mono"
          autoFocus
        />
      </div>
      <input
        type="text"
        value={formDescription}
        onChange={(e) => setFormDescription(e.target.value)}
        placeholder="Description of this endpoint/interface"
        className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600"
      />
      <div className="grid grid-cols-2 gap-2">
        <div>
          <label className="text-[10px] text-gray-500 dark:text-gray-400 block mb-0.5">Request Body (JSON)</label>
          <textarea
            value={formRequestBody}
            onChange={(e) => setFormRequestBody(e.target.value)}
            placeholder='{"field": "type"}'
            className="w-full px-2 py-1.5 border border-gray-300 rounded-lg text-xs font-mono dark:bg-gray-700 dark:border-gray-600 resize-none"
            rows={3}
          />
        </div>
        <div>
          <label className="text-[10px] text-gray-500 dark:text-gray-400 block mb-0.5">Response Success (JSON)</label>
          <textarea
            value={formResponseSuccess}
            onChange={(e) => setFormResponseSuccess(e.target.value)}
            placeholder='{"status": 200, "body": {}}'
            className="w-full px-2 py-1.5 border border-gray-300 rounded-lg text-xs font-mono dark:bg-gray-700 dark:border-gray-600 resize-none"
            rows={3}
          />
        </div>
      </div>
      <div>
        <label className="text-[10px] text-gray-500 dark:text-gray-400 block mb-0.5">Response Errors (JSON array)</label>
        <textarea
          value={formResponseErrors}
          onChange={(e) => setFormResponseErrors(e.target.value)}
          placeholder='[{"status": 400, "error": "Bad Request"}, {"status": 404, "error": "Not Found"}]'
          className="w-full px-2 py-1.5 border border-gray-300 rounded-lg text-xs font-mono dark:bg-gray-700 dark:border-gray-600 resize-none"
          rows={2}
        />
      </div>
      <textarea
        value={formNotes}
        onChange={(e) => setFormNotes(e.target.value)}
        placeholder="Notes (optional)"
        className="w-full px-2 py-1.5 border border-gray-300 rounded-lg text-xs dark:bg-gray-700 dark:border-gray-600 resize-none"
        rows={1}
      />
      {frs.length > 0 && (
        <div>
          <span className="text-[10px] text-gray-500 dark:text-gray-400 block mb-1">Link to functional requirements:</span>
          <div className="flex flex-wrap gap-1">
            {frs.map((fr, i) => {
              const key = String(i);
              const isLinked = formLinkedFRs.includes(key);
              return (
                <button
                  key={i}
                  onClick={() => toggleFR(key)}
                  className={`text-[10px] px-1.5 py-0.5 rounded transition-colors ${
                    isLinked
                      ? 'bg-indigo-100 text-indigo-700 dark:bg-indigo-900/40 dark:text-indigo-300 ring-1 ring-indigo-400'
                      : 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400 hover:bg-gray-200'
                  }`}
                >
                  FR{i}: {fr.length > 40 ? fr.slice(0, 37) + '...' : fr}
                </button>
              );
            })}
          </div>
        </div>
      )}
      {brs.length > 0 && (
        <div>
          <span className="text-[10px] text-gray-500 dark:text-gray-400 block mb-1">Link to business rules:</span>
          <div className="flex flex-wrap gap-1">
            {brs.map((br) => {
              const isLinked = formLinkedBRs.includes(br.id);
              return (
                <button
                  key={br.id}
                  onClick={() => toggleBR(br.id)}
                  className={`text-[10px] px-1.5 py-0.5 rounded transition-colors ${
                    isLinked
                      ? 'bg-violet-100 text-violet-700 dark:bg-violet-900/40 dark:text-violet-300 ring-1 ring-violet-400'
                      : 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400 hover:bg-gray-200'
                  }`}
                >
                  {br.title}
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
      {/* Skip contract coverage toggle */}
      {onSpecUpdate && (
        <div className="flex items-center justify-between px-3 py-2 rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50/50 dark:bg-gray-700/20">
          <div>
            <span className="text-xs font-medium text-gray-700 dark:text-gray-300">Skip contract coverage requirement</span>
            <p className="text-[10px] text-gray-400">Allow validation without full API contract→Task coverage</p>
          </div>
          <button
            onClick={() => onSpecUpdate({ skip_contract_coverage: !spec.skip_contract_coverage })}
            className={`relative w-10 h-5 rounded-full transition-colors ${spec.skip_contract_coverage ? 'bg-amber-500' : 'bg-gray-300 dark:bg-gray-600'}`}
          >
            <span className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white transition-transform ${spec.skip_contract_coverage ? 'translate-x-5' : ''}`} />
          </button>
        </div>
      )}

      {/* Contracts list */}
      {contracts.length === 0 && !adding && (
        <div className="text-center py-6">
          <FileCode size={32} className="mx-auto text-gray-300 dark:text-gray-600 mb-2" />
          <p className="text-sm text-gray-500 dark:text-gray-400">No API contracts defined</p>
          <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">Contracts define how components and services communicate</p>
        </div>
      )}

      {contracts.map((contract) => {
        const isExpanded = expandedId === contract.id;
        const isEditing = editingId === contract.id;
        const methodColor = METHOD_COLORS[contract.method] || 'bg-gray-500 text-white';

        if (isEditing) {
          return (
            <div key={contract.id}>
              {renderForm(handleSaveEdit, 'Save', () => { setEditingId(null); resetForm(); })}
            </div>
          );
        }

        return (
          <div key={contract.id} className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
            <div
              className="flex items-center gap-2 px-3 py-2 cursor-pointer bg-gray-50 dark:bg-gray-700/50"
              onClick={() => setExpandedId(isExpanded ? null : contract.id)}
            >
              <span className={`text-[10px] px-1.5 py-0.5 rounded font-mono font-bold shrink-0 ${methodColor}`}>
                {contract.method}
              </span>
              <span className="text-sm font-mono text-gray-700 dark:text-gray-300 truncate flex-1">
                {contract.path}
              </span>
              <span className="text-xs text-gray-500 dark:text-gray-400 truncate max-w-[200px] hidden sm:inline">
                {contract.description}
              </span>
              {(contract.linked_task_ids?.length ?? 0) > 0 ? (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300 shrink-0">
                  {contract.linked_task_ids!.length} task{contract.linked_task_ids!.length !== 1 ? 's' : ''}
                </span>
              ) : (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-100 text-gray-400 dark:bg-gray-700 dark:text-gray-500 shrink-0">
                  0 tasks
                </span>
              )}
              <button
                onClick={(e) => { e.stopPropagation(); handleEdit(contract); }}
                className="p-0.5 text-gray-400 hover:text-blue-500"
              >
                <Pencil size={12} />
              </button>
              <button
                onClick={(e) => { e.stopPropagation(); handleRemove(contract.id); }}
                className="p-0.5 text-gray-400 hover:text-red-500"
              >
                <Trash2 size={12} />
              </button>
              {isExpanded ? <ChevronUp size={14} className="text-gray-400" /> : <ChevronDown size={14} className="text-gray-400" />}
            </div>
            {isExpanded && (
              <div className="px-3 py-2 space-y-2 text-sm">
                <div>
                  <span className="text-[10px] font-semibold text-gray-500 uppercase">Path</span>
                  <p className="text-xs font-mono text-gray-700 dark:text-gray-300 mt-0.5">{contract.path}</p>
                </div>
                <div>
                  <span className="text-[10px] font-semibold text-gray-500 uppercase">Description</span>
                  <p className="text-xs text-gray-600 dark:text-gray-400 mt-0.5">{contract.description}</p>
                </div>
                {contract.request_body && (
                  <div>
                    <span className="text-[10px] font-semibold text-blue-600 uppercase">Request Body</span>
                    <pre className="mt-0.5 text-xs text-gray-600 dark:text-gray-400 bg-gray-50 dark:bg-gray-800 rounded p-2 overflow-x-auto whitespace-pre-wrap max-h-32 overflow-y-auto font-mono">
                      {JSON.stringify(contract.request_body, null, 2)}
                    </pre>
                  </div>
                )}
                {contract.response_success && (
                  <div>
                    <span className="text-[10px] font-semibold text-green-600 uppercase">Response (Success)</span>
                    <pre className="mt-0.5 text-xs text-gray-600 dark:text-gray-400 bg-gray-50 dark:bg-gray-800 rounded p-2 overflow-x-auto whitespace-pre-wrap max-h-32 overflow-y-auto font-mono">
                      {JSON.stringify(contract.response_success, null, 2)}
                    </pre>
                  </div>
                )}
                {contract.response_errors && contract.response_errors.length > 0 && (
                  <div>
                    <span className="text-[10px] font-semibold text-red-600 uppercase">Error Responses</span>
                    <div className="mt-0.5 space-y-1">
                      {contract.response_errors.map((err, i) => (
                        <pre key={i} className="text-xs text-gray-600 dark:text-gray-400 bg-red-50 dark:bg-red-900/10 rounded p-1.5 overflow-x-auto whitespace-pre-wrap font-mono">
                          {JSON.stringify(err, null, 2)}
                        </pre>
                      ))}
                    </div>
                  </div>
                )}
                {contract.notes && (
                  <p className="text-xs text-gray-500 dark:text-gray-400 italic border-l-2 border-gray-300 dark:border-gray-600 pl-2">{contract.notes}</p>
                )}
                {contract.linked_requirements && contract.linked_requirements.length > 0 && (
                  <div className="flex flex-wrap gap-1">
                    <span className="text-[10px] text-gray-400 mr-1">Linked FRs:</span>
                    {contract.linked_requirements.map((idx, i) => {
                      const frIdx = parseInt(idx, 10);
                      const frText = frs[frIdx];
                      return (
                        <span key={i} className="text-[10px] px-1.5 py-0.5 rounded bg-indigo-50 text-indigo-700 dark:bg-indigo-900/20 dark:text-indigo-300">
                          FR{idx}{frText ? `: ${frText.length > 30 ? frText.slice(0, 27) + '...' : frText}` : ''}
                        </span>
                      );
                    })}
                  </div>
                )}
                {contract.linked_rules && contract.linked_rules.length > 0 && (
                  <div className="flex flex-wrap gap-1">
                    <span className="text-[10px] text-gray-400 mr-1">Linked BRs:</span>
                    {contract.linked_rules.map((brId, i) => {
                      const br = brs.find((b) => b.id === brId);
                      return (
                        <span key={i} className="text-[10px] px-1.5 py-0.5 rounded bg-violet-50 text-violet-700 dark:bg-violet-900/20 dark:text-violet-300">
                          {br ? br.title : brId}
                        </span>
                      );
                    })}
                  </div>
                )}
                {/* Linked tasks with link/unlink controls */}
                <div>
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-[10px] text-gray-400">Linked Tasks:</span>
                  </div>
                  {contract.linked_task_ids && contract.linked_task_ids.length > 0 ? (
                    <div className="space-y-1">
                      {contract.linked_task_ids.map((taskId) => {
                        const card = specCards?.find((c) => c.id === taskId);
                        return (
                          <div key={taskId} className="flex items-center justify-between px-2 py-1 rounded bg-green-50 dark:bg-green-900/10 text-xs group">
                            <span className="text-gray-700 dark:text-gray-300 truncate">
                              {card ? card.title : taskId.slice(0, 12) + '...'}
                            </span>
                            <div className="flex items-center gap-1">
                              {card && (
                                <span className={`text-[10px] px-1 py-0.5 rounded ${
                                  card.status === 'done' ? 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300' :
                                  card.status === 'in_progress' ? 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300' :
                                  'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400'
                                }`}>
                                  {card.status.replace('_', ' ')}
                                </span>
                              )}
                              {onUnlinkTask && (
                                <button
                                  onClick={async () => { await onUnlinkTask(contract.id, taskId); }}
                                  className="p-0.5 text-gray-400 hover:text-red-500 opacity-0 group-hover:opacity-100 transition-opacity"
                                  title="Unlink task"
                                >
                                  <Unlink size={10} />
                                </button>
                              )}
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  ) : (
                    <p className="text-[10px] text-gray-400 italic">No tasks linked</p>
                  )}
                  {onLinkTask && specCards && specCards.length > 0 && (
                    <LinkTaskPicker
                      contractId={contract.id}
                      linkedIds={contract.linked_task_ids || []}
                      cards={specCards}
                      onLink={onLinkTask}
                    />
                  )}
                </div>
              </div>
            )}
          </div>
        );
      })}

      {/* Add form */}
      {adding ? (
        renderForm(handleAdd, 'Add Contract', () => { setAdding(false); resetForm(); })
      ) : (
        !editingId && (
          <button onClick={() => setAdding(true)} className="flex items-center gap-1 text-sm text-cyan-600 dark:text-cyan-400 hover:text-cyan-800 dark:hover:text-cyan-300">
            <Plus size={14} /> Add API Contract
          </button>
        )
      )}
    </div>
  );
}
