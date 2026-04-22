/**
 * RulesTab - Business rules management for specs
 */

import { useState, useMemo } from 'react';
import { Plus, Trash2, ChevronDown, ChevronUp, Scale, Pencil, CheckCircle, XCircle } from 'lucide-react';
import type { Spec, BusinessRule } from '@/types';

interface RulesTabProps {
  spec: Spec;
  onUpdate: (rules: BusinessRule[]) => void;
  onSpecUpdate?: (patch: Record<string, any>) => void;
}

export function RulesTab({ spec, onUpdate, onSpecUpdate }: RulesTabProps) {
  const [adding, setAdding] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  // Form state
  const [formTitle, setFormTitle] = useState('');
  const [formRule, setFormRule] = useState('');
  const [formWhen, setFormWhen] = useState('');
  const [formThen, setFormThen] = useState('');
  const [formNotes, setFormNotes] = useState('');
  const [formLinkedFRs, setFormLinkedFRs] = useState<string[]>([]);

  const rules = spec.business_rules || [];
  const frs = spec.functional_requirements || [];

  const resetForm = () => {
    setFormTitle('');
    setFormRule('');
    setFormWhen('');
    setFormThen('');
    setFormNotes('');
    setFormLinkedFRs([]);
  };

  const handleAdd = () => {
    if (!formTitle.trim() || !formRule.trim() || !formWhen.trim() || !formThen.trim()) return;
    const id = `br_${Date.now()}`;
    const rule: BusinessRule = {
      id,
      title: formTitle.trim(),
      rule: formRule.trim(),
      when: formWhen.trim(),
      then: formThen.trim(),
      linked_requirements: formLinkedFRs.length > 0 ? formLinkedFRs : null,
      linked_task_ids: null,
      notes: formNotes.trim() || null,
    };
    onUpdate([...rules, rule]);
    setAdding(false);
    resetForm();
  };

  const handleEdit = (rule: BusinessRule) => {
    setEditingId(rule.id);
    setFormTitle(rule.title);
    setFormRule(rule.rule);
    setFormWhen(rule.when);
    setFormThen(rule.then);
    setFormNotes(rule.notes || '');
    setFormLinkedFRs(rule.linked_requirements || []);
  };

  const handleSaveEdit = () => {
    if (!editingId || !formTitle.trim() || !formRule.trim() || !formWhen.trim() || !formThen.trim()) return;
    onUpdate(rules.map((r) =>
      r.id === editingId
        ? {
            ...r,
            title: formTitle.trim(),
            rule: formRule.trim(),
            when: formWhen.trim(),
            then: formThen.trim(),
            linked_requirements: formLinkedFRs.length > 0 ? formLinkedFRs : null,
            notes: formNotes.trim() || null,
          }
        : r
    ));
    setEditingId(null);
    resetForm();
  };

  const handleRemove = (id: string) => {
    if (!confirm('Remove this business rule?')) return;
    onUpdate(rules.filter((r) => r.id !== id));
  };

  const toggleFR = (fr: string) => {
    setFormLinkedFRs((prev) =>
      prev.includes(fr) ? prev.filter((x) => x !== fr) : [...prev, fr]
    );
  };

  const isFormValid = formTitle.trim() && formRule.trim() && formWhen.trim() && formThen.trim();

  const renderForm = (onSubmit: () => void, submitLabel: string, onCancel: () => void) => (
    <div className="border border-indigo-200 dark:border-indigo-700 rounded-lg p-3 space-y-2 bg-indigo-50/50 dark:bg-indigo-900/10">
      <input
        type="text"
        value={formTitle}
        onChange={(e) => setFormTitle(e.target.value)}
        placeholder="Rule title"
        className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600"
        autoFocus
      />
      <textarea
        value={formRule}
        onChange={(e) => setFormRule(e.target.value)}
        placeholder="Rule description — what must be enforced"
        className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600 resize-none"
        rows={2}
      />
      <div className="grid grid-cols-2 gap-2">
        <input
          type="text"
          value={formWhen}
          onChange={(e) => setFormWhen(e.target.value)}
          placeholder="When: condition..."
          className="px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600"
        />
        <input
          type="text"
          value={formThen}
          onChange={(e) => setFormThen(e.target.value)}
          placeholder="Then: action/result..."
          className="px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600"
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
                  FR{i}: {fr.length > 50 ? fr.slice(0, 47) + '...' : fr}
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

  // Compute FR coverage — linked_requirements can be indices ("0") or full FR text
  const frCoverage = useMemo(() => {
    const coveredIndices = new Set<number>();
    for (const br of rules) {
      for (const ref of (br.linked_requirements || [])) {
        const refStr = String(ref);
        // Try as numeric index first
        const asNum = parseInt(refStr, 10);
        if (!isNaN(asNum) && asNum >= 0 && asNum < frs.length) {
          coveredIndices.add(asNum);
        } else {
          // Try matching by FR text content
          const idx = frs.findIndex((fr) => refStr.includes(fr) || fr.includes(refStr));
          if (idx >= 0) coveredIndices.add(idx);
        }
      }
    }
    return frs.map((fr, i) => ({
      index: i,
      text: fr,
      covered: coveredIndices.has(i),
    }));
  }, [rules, frs]);

  const coveredCount = frCoverage.filter(f => f.covered).length;

  return (
    <div className="space-y-4">
      {/* FR Coverage summary */}
      {frs.length > 0 && (
        <div className="border border-gray-200 dark:border-gray-700 rounded-lg p-3">
          <div className="flex items-center justify-between mb-2">
            <h4 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide">
              FR Coverage ({coveredCount}/{frs.length})
            </h4>
            {coveredCount === frs.length ? (
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300 font-medium">
                100% covered
              </span>
            ) : (
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300 font-medium">
                {frs.length > 0 ? Math.round((coveredCount / frs.length) * 100) : 0}% covered
              </span>
            )}
          </div>
          {/* Progress bar */}
          <div className="h-2 bg-gray-100 dark:bg-gray-700 rounded-full overflow-hidden mb-2">
            <div
              className={`h-full transition-all duration-500 rounded-full ${coveredCount === frs.length ? 'bg-green-500' : 'bg-amber-500'}`}
              style={{ width: `${frs.length > 0 ? (coveredCount / frs.length) * 100 : 0}%` }}
            />
          </div>
          {/* FR list */}
          <div className="space-y-1 max-h-48 overflow-y-auto">
            {frCoverage.map((fr) => (
              <div key={fr.index} className="flex items-start gap-2 text-xs">
                {fr.covered ? (
                  <CheckCircle className="w-3.5 h-3.5 text-green-500 shrink-0 mt-0.5" />
                ) : (
                  <XCircle className="w-3.5 h-3.5 text-gray-300 dark:text-gray-600 shrink-0 mt-0.5" />
                )}
                <span className={`${fr.covered ? 'text-gray-700 dark:text-gray-300' : 'text-gray-400 dark:text-gray-500'} line-clamp-1`}>
                  <span className="font-medium">FR{fr.index}:</span> {fr.text}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Skip rules coverage toggle */}
      {onSpecUpdate && (
        <div className="flex items-center justify-between px-3 py-2 rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50/50 dark:bg-gray-700/20">
          <div>
            <span className="text-xs font-medium text-gray-700 dark:text-gray-300">Skip rules coverage requirement</span>
            <p className="text-[10px] text-gray-400">Allow starting cards without full FR→BR coverage</p>
          </div>
          <button
            onClick={() => onSpecUpdate({ skip_rules_coverage: !(spec as any).skip_rules_coverage })}
            className={`relative w-10 h-5 rounded-full transition-colors ${(spec as any).skip_rules_coverage ? 'bg-amber-500' : 'bg-gray-300 dark:bg-gray-600'}`}
          >
            <span className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white transition-transform ${(spec as any).skip_rules_coverage ? 'translate-x-5' : ''}`} />
          </button>
        </div>
      )}

      {/* Rules list */}
      {rules.length === 0 && !adding && (
        <div className="text-center py-6">
          <Scale size={32} className="mx-auto text-gray-300 dark:text-gray-600 mb-2" />
          <p className="text-sm text-gray-500 dark:text-gray-400">No business rules defined</p>
          <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">Business rules capture validations and conditional behaviors from requirements</p>
        </div>
      )}

      {rules.map((rule) => {
        const isExpanded = expandedId === rule.id;
        const isEditing = editingId === rule.id;

        if (isEditing) {
          return (
            <div key={rule.id}>
              {renderForm(handleSaveEdit, 'Save', () => { setEditingId(null); resetForm(); })}
            </div>
          );
        }

        return (
          <div key={rule.id} className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
            <div
              className="flex items-center gap-2 px-3 py-2 cursor-pointer bg-gray-50 dark:bg-gray-700/50"
              onClick={() => setExpandedId(isExpanded ? null : rule.id)}
            >
              <Scale size={14} className="text-indigo-500 shrink-0" />
              <span className="text-sm font-medium text-gray-900 dark:text-white truncate flex-1">{rule.title}</span>
              {rule.linked_requirements && rule.linked_requirements.length > 0 && (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-indigo-100 text-indigo-700 dark:bg-indigo-900/40 dark:text-indigo-300">
                  {rule.linked_requirements.length} FR{rule.linked_requirements.length !== 1 ? 's' : ''}
                </span>
              )}
              {(rule.linked_task_ids?.length ?? 0) > 0 ? (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300">
                  {rule.linked_task_ids!.length} task{rule.linked_task_ids!.length !== 1 ? 's' : ''}
                </span>
              ) : (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-100 text-gray-400 dark:bg-gray-700 dark:text-gray-500">
                  0 tasks
                </span>
              )}
              <button
                onClick={(e) => { e.stopPropagation(); handleEdit(rule); }}
                className="p-0.5 text-gray-400 hover:text-blue-500"
              >
                <Pencil size={12} />
              </button>
              <button
                onClick={(e) => { e.stopPropagation(); handleRemove(rule.id); }}
                className="p-0.5 text-gray-400 hover:text-red-500"
              >
                <Trash2 size={12} />
              </button>
              {isExpanded ? <ChevronUp size={14} className="text-gray-400" /> : <ChevronDown size={14} className="text-gray-400" />}
            </div>
            {isExpanded && (
              <div className="px-3 py-2 space-y-2 text-sm">
                <p className="text-xs text-gray-600 dark:text-gray-400">{rule.rule}</p>
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <span className="text-[10px] font-semibold text-amber-600 uppercase">When</span>
                    <p className="text-xs text-gray-600 dark:text-gray-400 mt-0.5">{rule.when}</p>
                  </div>
                  <div>
                    <span className="text-[10px] font-semibold text-green-600 uppercase">Then</span>
                    <p className="text-xs text-gray-600 dark:text-gray-400 mt-0.5">{rule.then}</p>
                  </div>
                </div>
                {rule.notes && (
                  <p className="text-xs text-gray-500 dark:text-gray-400 italic border-l-2 border-gray-300 dark:border-gray-600 pl-2">{rule.notes}</p>
                )}
                {rule.linked_requirements && rule.linked_requirements.length > 0 && (
                  <div className="flex flex-wrap gap-1">
                    <span className="text-[10px] text-gray-400 mr-1">Linked FRs:</span>
                    {rule.linked_requirements.map((idx, i) => {
                      const frIdx = parseInt(idx, 10);
                      const frText = frs[frIdx];
                      return (
                        <span key={i} className="text-[10px] px-1.5 py-0.5 rounded bg-indigo-50 text-indigo-700 dark:bg-indigo-900/20 dark:text-indigo-300">
                          FR{idx}{frText ? `: ${frText.length > 40 ? frText.slice(0, 37) + '...' : frText}` : ''}
                        </span>
                      );
                    })}
                  </div>
                )}
                {rule.linked_task_ids && rule.linked_task_ids.length > 0 && (
                  <div className="flex flex-wrap gap-1">
                    <span className="text-[10px] text-gray-400 mr-1">Linked Tasks:</span>
                    {rule.linked_task_ids.map((taskId, i) => (
                      <span key={i} className="text-[10px] px-1.5 py-0.5 rounded bg-green-50 text-green-700 dark:bg-green-900/20 dark:text-green-300">
                        {taskId.slice(0, 8)}…
                      </span>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        );
      })}

      {/* Add form */}
      {adding ? (
        renderForm(handleAdd, 'Add Rule', () => { setAdding(false); resetForm(); })
      ) : (
        !editingId && (
          <button onClick={() => setAdding(true)} className="flex items-center gap-1 text-sm text-indigo-600 dark:text-indigo-400 hover:text-indigo-800 dark:hover:text-indigo-300">
            <Plus size={14} /> Add Business Rule
          </button>
        )
      )}
    </div>
  );
}
