/**
 * DecisionsTab — structured management for Decisions on a spec.
 *
 * Ideação #10 Fase 3: paridade end-to-end com TR/BR/Contract.
 * Mirrors RulesTab pattern — form to add/edit, expandable rows with badge
 * colored by status (active/superseded/revoked), link-to-task picker,
 * spec-level skip_decisions_coverage toggle.
 */

import { useMemo, useState } from 'react';
import {
  CheckCircle, ChevronDown, ChevronUp, GitBranch, Pencil, Plus, Trash2, XCircle,
} from 'lucide-react';
import type { Decision, DecisionStatus, Spec } from '@/types';

interface DecisionsTabProps {
  spec: Spec;
  onUpdate: (decisions: Decision[]) => void;
  onSpecUpdate?: (patch: Record<string, any>) => void;
  specCards?: Array<{ id: string; title: string }>;
  onLinkTask?: (decisionId: string, cardId: string) => void | Promise<void>;
  onUnlinkTask?: (decisionId: string, cardId: string) => void | Promise<void>;
}

const STATUS_COLORS: Record<DecisionStatus, string> = {
  active: 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300 ring-1 ring-green-400',
  superseded: 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
  revoked: 'bg-red-100 text-red-600 dark:bg-red-900/30 dark:text-red-400',
};

export function DecisionsTab({
  spec,
  onUpdate,
  onSpecUpdate,
  specCards = [],
  onLinkTask,
  onUnlinkTask,
}: DecisionsTabProps) {
  const [adding, setAdding] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [linkPickerId, setLinkPickerId] = useState<string | null>(null);

  const [formTitle, setFormTitle] = useState('');
  const [formRationale, setFormRationale] = useState('');
  const [formContext, setFormContext] = useState('');
  const [formAlternatives, setFormAlternatives] = useState('');
  const [formSupersedesId, setFormSupersedesId] = useState('');
  const [formLinkedFRs, setFormLinkedFRs] = useState<string[]>([]);
  const [formNotes, setFormNotes] = useState('');

  const decisions = spec.decisions || [];
  const frs = spec.functional_requirements || [];

  const resetForm = () => {
    setFormTitle('');
    setFormRationale('');
    setFormContext('');
    setFormAlternatives('');
    setFormSupersedesId('');
    setFormLinkedFRs([]);
    setFormNotes('');
  };

  const parseAlternatives = (raw: string): string[] | null => {
    const trimmed = raw.trim();
    if (!trimmed) return null;
    return trimmed.split(/\n|;|\|/).map((x) => x.trim()).filter(Boolean);
  };

  const handleAdd = () => {
    if (!formTitle.trim() || !formRationale.trim()) return;
    const id = `dec_${Date.now()}`;
    const newDecision: Decision = {
      id,
      title: formTitle.trim(),
      rationale: formRationale.trim(),
      context: formContext.trim() || null,
      alternatives_considered: parseAlternatives(formAlternatives),
      supersedes_decision_id: formSupersedesId || null,
      linked_requirements: formLinkedFRs.length > 0 ? formLinkedFRs : null,
      linked_task_ids: null,
      status: 'active',
      notes: formNotes.trim() || null,
    };
    // Auto-superseding: if new.supersedes references an existing active decision,
    // flip that one's status to 'superseded' to keep the chain coherent.
    let next = [...decisions, newDecision];
    if (formSupersedesId) {
      next = next.map((d) =>
        d.id === formSupersedesId && d.status === 'active' ? { ...d, status: 'superseded' as DecisionStatus } : d
      );
    }
    onUpdate(next);
    setAdding(false);
    resetForm();
  };

  const handleEdit = (d: Decision) => {
    setEditingId(d.id);
    setFormTitle(d.title);
    setFormRationale(d.rationale);
    setFormContext(d.context || '');
    setFormAlternatives((d.alternatives_considered || []).join('\n'));
    setFormSupersedesId(d.supersedes_decision_id || '');
    setFormLinkedFRs(d.linked_requirements || []);
    setFormNotes(d.notes || '');
  };

  const handleSaveEdit = () => {
    if (!editingId || !formTitle.trim() || !formRationale.trim()) return;
    onUpdate(decisions.map((d) =>
      d.id === editingId
        ? {
            ...d,
            title: formTitle.trim(),
            rationale: formRationale.trim(),
            context: formContext.trim() || null,
            alternatives_considered: parseAlternatives(formAlternatives),
            supersedes_decision_id: formSupersedesId || null,
            linked_requirements: formLinkedFRs.length > 0 ? formLinkedFRs : null,
            notes: formNotes.trim() || null,
          }
        : d
    ));
    setEditingId(null);
    resetForm();
  };

  const handleRevoke = (id: string) => {
    if (!confirm('Revoke this decision? Soft-delete — it stays in the history with status=revoked.')) return;
    onUpdate(decisions.map((d) => (d.id === id ? { ...d, status: 'revoked' as DecisionStatus } : d)));
  };

  const toggleFR = (key: string) => {
    setFormLinkedFRs((prev) => (prev.includes(key) ? prev.filter((x) => x !== key) : [...prev, key]));
  };

  const availableSupersedesTargets = useMemo(
    () => decisions.filter((d) => d.id !== editingId && d.status !== 'revoked'),
    [decisions, editingId]
  );

  const isFormValid = formTitle.trim() && formRationale.trim();

  const activeDecisions = decisions.filter((d) => d.status === 'active');
  const activeTotal = activeDecisions.length;
  const activeLinked = activeDecisions.filter((d) => (d.linked_task_ids?.length ?? 0) > 0).length;
  const coveragePct = activeTotal === 0 ? 100 : Math.round((activeLinked / activeTotal) * 100);

  const renderForm = (onSubmit: () => void, submitLabel: string, onCancel: () => void) => (
    <div className="border border-indigo-200 dark:border-indigo-700 rounded-lg p-3 space-y-2 bg-indigo-50/50 dark:bg-indigo-900/10">
      <input
        type="text"
        value={formTitle}
        onChange={(e) => setFormTitle(e.target.value)}
        placeholder="Decision title — e.g. 'Use embedded graph storage over an external graph database'"
        className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600"
        autoFocus
      />
      <textarea
        value={formRationale}
        onChange={(e) => setFormRationale(e.target.value)}
        placeholder="Rationale — why this choice was made"
        className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600 resize-none"
        rows={2}
      />
      <textarea
        value={formContext}
        onChange={(e) => setFormContext(e.target.value)}
        placeholder="Context (optional) — when/where the decision applies"
        className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600 resize-none"
        rows={1}
      />
      <textarea
        value={formAlternatives}
        onChange={(e) => setFormAlternatives(e.target.value)}
        placeholder="Alternatives considered (one per line, or separated by ; or |)"
        className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600 resize-none"
        rows={2}
      />
      {availableSupersedesTargets.length > 0 && (
        <select
          value={formSupersedesId}
          onChange={(e) => setFormSupersedesId(e.target.value)}
          className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600"
        >
          <option value="">(Optional) Supersedes… — pick an existing decision</option>
          {availableSupersedesTargets.map((d) => (
            <option key={d.id} value={d.id}>
              {d.title}
            </option>
          ))}
        </select>
      )}
      <textarea
        value={formNotes}
        onChange={(e) => setFormNotes(e.target.value)}
        placeholder="Notes (optional)"
        className="w-full px-2 py-1.5 border border-gray-300 rounded-lg text-xs dark:bg-gray-700 dark:border-gray-600 resize-none"
        rows={1}
      />
      {frs.length > 0 && (
        <div>
          <span className="text-[10px] text-gray-500 dark:text-gray-400 block mb-1">
            Link to functional requirements:
          </span>
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
                  FR{i}: {fr.length > 50 ? fr.slice(0, 47) + '...' : fr}
                </button>
              );
            })}
          </div>
        </div>
      )}
      <div className="flex justify-end gap-2">
        <button onClick={onCancel} className="btn btn-secondary text-xs">Cancel</button>
        <button onClick={onSubmit} disabled={!isFormValid} className="btn btn-primary text-xs">
          {submitLabel}
        </button>
      </div>
    </div>
  );

  return (
    <div className="space-y-4">
      {/* Legacy banner — decisions inline em Description ficam visíveis, mas esta aba é canônica */}
      <div className="rounded-md border border-indigo-200 dark:border-indigo-700 bg-indigo-50/60 dark:bg-indigo-900/10 px-3 py-2 text-xs text-indigo-700 dark:text-indigo-300">
        Decisions estruturadas têm cobertura de tasks e validação semântica.
        Listagem inline em Description é legado — use esta aba para gerenciamento.
      </div>

      {/* Coverage summary — active-only */}
      {activeTotal > 0 && (
        <div className="border border-gray-200 dark:border-gray-700 rounded-lg p-3">
          <div className="flex items-center justify-between mb-2">
            <h4 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide">
              Active Decision Coverage ({activeLinked}/{activeTotal})
            </h4>
            <span
              className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${
                coveragePct === 100
                  ? 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300'
                  : 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300'
              }`}
            >
              {coveragePct}% covered
            </span>
          </div>
          <div className="h-2 bg-gray-100 dark:bg-gray-700 rounded-full overflow-hidden">
            <div
              className={`h-full transition-all duration-500 rounded-full ${
                coveragePct === 100 ? 'bg-green-500' : 'bg-amber-500'
              }`}
              style={{ width: `${coveragePct}%` }}
            />
          </div>
        </div>
      )}

      {/* Skip toggle — spec level */}
      {onSpecUpdate && (
        <div className="flex items-center justify-between px-3 py-2 rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50/50 dark:bg-gray-700/20">
          <div>
            <span className="text-xs font-medium text-gray-700 dark:text-gray-300">Skip decisions coverage</span>
            <p className="text-[10px] text-gray-400">
              Allow submit_spec_validation without requiring each active Decision to be linked to a task
            </p>
          </div>
          <button
            onClick={() =>
              onSpecUpdate({ skip_decisions_coverage: !(spec as any).skip_decisions_coverage })
            }
            className={`relative w-10 h-5 rounded-full transition-colors ${
              (spec as any).skip_decisions_coverage ? 'bg-amber-500' : 'bg-gray-300 dark:bg-gray-600'
            }`}
          >
            <span
              className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white transition-transform ${
                (spec as any).skip_decisions_coverage ? 'translate-x-5' : ''
              }`}
            />
          </button>
        </div>
      )}

      {/* Empty state */}
      {decisions.length === 0 && !adding && (
        <div className="text-center py-6">
          <GitBranch size={32} className="mx-auto text-gray-300 dark:text-gray-600 mb-2" />
          <p className="text-sm text-gray-500 dark:text-gray-400">No decisions recorded</p>
          <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">
            Decisions capture the <em>why</em> behind design choices, with alternatives and supersedence
          </p>
        </div>
      )}

      {/* Decisions list */}
      {decisions.map((d) => {
        const isExpanded = expandedId === d.id;
        const isEditing = editingId === d.id;

        if (isEditing) {
          return (
            <div key={d.id}>
              {renderForm(handleSaveEdit, 'Save', () => { setEditingId(null); resetForm(); })}
            </div>
          );
        }

        const linkedTasksCount = d.linked_task_ids?.length ?? 0;
        const isActive = d.status === 'active';
        const needsLinkage = isActive && linkedTasksCount === 0;

        return (
          <div key={d.id} className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
            <div
              className="flex items-center gap-2 px-3 py-2 cursor-pointer bg-gray-50 dark:bg-gray-700/50"
              onClick={() => setExpandedId(isExpanded ? null : d.id)}
            >
              <GitBranch size={14} className="text-indigo-500 shrink-0" />
              <span className="text-sm font-medium text-gray-900 dark:text-white truncate flex-1">
                {d.title}
              </span>
              <span className={`text-[10px] px-1.5 py-0.5 rounded ${STATUS_COLORS[d.status]}`}>
                {d.status}
              </span>
              {(d.linked_requirements?.length ?? 0) > 0 && (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-indigo-100 text-indigo-700 dark:bg-indigo-900/40 dark:text-indigo-300">
                  {d.linked_requirements!.length} FR
                </span>
              )}
              {linkedTasksCount > 0 ? (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300">
                  {linkedTasksCount} task{linkedTasksCount !== 1 ? 's' : ''}
                </span>
              ) : (
                <span
                  className={`text-[10px] px-1.5 py-0.5 rounded ${
                    needsLinkage
                      ? 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300'
                      : 'bg-gray-100 text-gray-400 dark:bg-gray-700 dark:text-gray-500'
                  }`}
                >
                  0 tasks
                </span>
              )}
              <button
                onClick={(e) => { e.stopPropagation(); handleEdit(d); }}
                className="p-0.5 text-gray-400 hover:text-blue-500"
                title="Edit"
              >
                <Pencil size={12} />
              </button>
              {d.status !== 'revoked' && (
                <button
                  onClick={(e) => { e.stopPropagation(); handleRevoke(d.id); }}
                  className="p-0.5 text-gray-400 hover:text-red-500"
                  title="Revoke (soft-delete)"
                >
                  <Trash2 size={12} />
                </button>
              )}
              {isExpanded ? (
                <ChevronUp size={14} className="text-gray-400" />
              ) : (
                <ChevronDown size={14} className="text-gray-400" />
              )}
            </div>

            {isExpanded && (
              <div className="px-3 py-2 space-y-2 text-sm">
                <div>
                  <span className="text-[10px] font-semibold text-gray-500 uppercase">Rationale</span>
                  <p className="text-xs text-gray-600 dark:text-gray-400 mt-0.5">{d.rationale}</p>
                </div>
                {d.context && (
                  <div>
                    <span className="text-[10px] font-semibold text-gray-500 uppercase">Context</span>
                    <p className="text-xs text-gray-600 dark:text-gray-400 mt-0.5">{d.context}</p>
                  </div>
                )}
                {d.alternatives_considered && d.alternatives_considered.length > 0 && (
                  <div>
                    <span className="text-[10px] font-semibold text-gray-500 uppercase">Alternatives</span>
                    <ul className="text-xs text-gray-600 dark:text-gray-400 mt-0.5 list-disc list-inside">
                      {d.alternatives_considered.map((alt, i) => (
                        <li key={i}>{alt}</li>
                      ))}
                    </ul>
                  </div>
                )}
                {d.supersedes_decision_id && (
                  <div className="flex items-center gap-1 text-xs text-amber-600 dark:text-amber-400">
                    <GitBranch size={12} />
                    Supersedes: <code className="text-[10px]">{d.supersedes_decision_id}</code>
                  </div>
                )}
                {d.linked_requirements && d.linked_requirements.length > 0 && (
                  <div className="flex flex-wrap gap-1">
                    <span className="text-[10px] text-gray-400 mr-1">Linked FRs:</span>
                    {d.linked_requirements.map((idx, i) => {
                      const n = parseInt(idx, 10);
                      const txt = !isNaN(n) ? frs[n] : undefined;
                      return (
                        <span
                          key={i}
                          className="text-[10px] px-1.5 py-0.5 rounded bg-indigo-50 text-indigo-700 dark:bg-indigo-900/20 dark:text-indigo-300"
                        >
                          FR{idx}{txt ? `: ${txt.length > 40 ? txt.slice(0, 37) + '...' : txt}` : ''}
                        </span>
                      );
                    })}
                  </div>
                )}
                {d.linked_task_ids && d.linked_task_ids.length > 0 && (
                  <div className="flex flex-wrap gap-1 items-center">
                    <span className="text-[10px] text-gray-400 mr-1">Linked tasks:</span>
                    {d.linked_task_ids.map((tid, i) => (
                      <span
                        key={i}
                        className="text-[10px] px-1.5 py-0.5 rounded bg-green-50 text-green-700 dark:bg-green-900/20 dark:text-green-300 inline-flex items-center gap-0.5"
                      >
                        {tid.slice(0, 8)}…
                        {onUnlinkTask && (
                          <button
                            onClick={() => onUnlinkTask(d.id, tid)}
                            className="ml-0.5 text-green-600 hover:text-red-500"
                            title="Unlink"
                          >
                            <XCircle size={10} />
                          </button>
                        )}
                      </span>
                    ))}
                  </div>
                )}
                {d.notes && (
                  <p className="text-xs text-gray-500 dark:text-gray-400 italic border-l-2 border-gray-300 dark:border-gray-600 pl-2">
                    {d.notes}
                  </p>
                )}

                {/* Task picker */}
                {onLinkTask && d.status !== 'revoked' && specCards.length > 0 && (
                  <div className="pt-1">
                    {linkPickerId === d.id ? (
                      <div className="space-y-1">
                        <span className="text-[10px] text-gray-500 block">Pick a card to link:</span>
                        <div className="flex flex-wrap gap-1 max-h-32 overflow-y-auto">
                          {specCards
                            .filter((c) => !(d.linked_task_ids || []).includes(c.id))
                            .map((c) => (
                              <button
                                key={c.id}
                                onClick={async () => {
                                  await onLinkTask(d.id, c.id);
                                  setLinkPickerId(null);
                                }}
                                className="text-[10px] px-1.5 py-0.5 rounded bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300 hover:bg-indigo-100 dark:hover:bg-indigo-900/40"
                              >
                                {c.title.length > 30 ? c.title.slice(0, 27) + '...' : c.title}
                              </button>
                            ))}
                        </div>
                        <button
                          onClick={() => setLinkPickerId(null)}
                          className="text-[10px] text-gray-500 hover:text-gray-700"
                        >
                          Cancel
                        </button>
                      </div>
                    ) : (
                      <button
                        onClick={() => setLinkPickerId(d.id)}
                        className="flex items-center gap-1 text-[10px] text-indigo-600 dark:text-indigo-400 hover:text-indigo-800 dark:hover:text-indigo-300"
                      >
                        <CheckCircle size={10} /> Link task…
                      </button>
                    )}
                  </div>
                )}
              </div>
            )}
          </div>
        );
      })}

      {/* Add form */}
      {adding ? (
        renderForm(handleAdd, 'Add Decision', () => { setAdding(false); resetForm(); })
      ) : (
        !editingId && (
          <button
            onClick={() => setAdding(true)}
            className="flex items-center gap-1 text-sm text-indigo-600 dark:text-indigo-400 hover:text-indigo-800 dark:hover:text-indigo-300"
          >
            <Plus size={14} /> Add Decision
          </button>
        )
      )}
    </div>
  );
}
