/**
 * ContextSelector - Select which parts of a parent entity to carry into a derived entity
 */

import { useState } from 'react';
import { Check, ChevronDown, ChevronUp } from 'lucide-react';
import { MarkdownContent } from './MarkdownContent';

export interface SelectableItem {
  id: string;
  label: string;
  content: string;
  category: string; // e.g. "Problem Statement", "Q&A", "Decision"
}

interface ContextSelectorProps {
  title: string;
  description: string;
  items: SelectableItem[];
  onConfirm: (selectedItems: SelectableItem[], title: string) => void;
  onCancel: () => void;
  targetLabel: string; // e.g. "Refinement", "Spec Draft"
}

export function ContextSelector({
  title: _title,
  description,
  items,
  onConfirm,
  onCancel,
  targetLabel,
}: ContextSelectorProps) {
  const [selected, setSelected] = useState<Set<string>>(new Set(items.map((i) => i.id)));
  const [entityTitle, setEntityTitle] = useState('');
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const toggle = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const selectAll = () => setSelected(new Set(items.map((i) => i.id)));
  const selectNone = () => setSelected(new Set());

  // Group items by category
  const categories = Array.from(new Set(items.map((i) => i.category)));
  const grouped = categories.map((cat) => ({
    category: cat,
    items: items.filter((i) => i.category === cat),
  }));

  const selectedItems = items.filter((i) => selected.has(i.id));

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-[70] p-4">
      <div className="bg-white dark:bg-gray-800 rounded-xl shadow-xl w-full max-w-2xl h-[85vh] flex flex-col">
        {/* Header */}
        <div className="px-6 py-4 border-b border-gray-200 dark:border-gray-700">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
            Create {targetLabel}
          </h2>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
            {description}
          </p>
        </div>

        {/* Title input */}
        <div className="px-6 py-3 border-b border-gray-100 dark:border-gray-700/50">
          <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">
            {targetLabel} Title <span className="text-red-500">*</span>
          </label>
          <input
            type="text"
            value={entityTitle}
            onChange={(e) => setEntityTitle(e.target.value)}
            placeholder={`What will this ${targetLabel.toLowerCase()} focus on?`}
            className={`w-full px-3 py-2 border rounded-lg text-sm dark:bg-gray-700 ${
              !entityTitle.trim()
                ? 'border-amber-400 dark:border-amber-600 ring-1 ring-amber-200 dark:ring-amber-800'
                : 'border-gray-300 dark:border-gray-600'
            }`}
            autoFocus
          />
          {!entityTitle.trim() && (
            <p className="text-xs text-amber-600 dark:text-amber-400 mt-1">Title is required to proceed</p>
          )}
        </div>

        {/* Selection controls */}
        <div className="px-6 py-2 border-b border-gray-100 dark:border-gray-700/50 flex items-center justify-between">
          <span className="text-xs text-gray-500 dark:text-gray-400">
            {selected.size} of {items.length} items selected
          </span>
          <div className="flex gap-2">
            <button onClick={selectAll} className="text-xs text-blue-600 dark:text-blue-400 hover:underline">Select all</button>
            <button onClick={selectNone} className="text-xs text-gray-500 dark:text-gray-400 hover:underline">Clear</button>
          </div>
        </div>

        {/* Selectable items */}
        <div className="flex-1 overflow-y-auto px-6 py-3 space-y-4">
          {grouped.map((group) => (
            <div key={group.category}>
              <h4 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-2">
                {group.category}
              </h4>
              <div className="space-y-1">
                {group.items.map((item) => {
                  const isSelected = selected.has(item.id);
                  const isExpanded = expandedId === item.id;
                  return (
                    <div
                      key={item.id}
                      className={`rounded-lg border transition-colors ${
                        isSelected
                          ? 'border-blue-300 dark:border-blue-600 bg-blue-50/50 dark:bg-blue-900/10'
                          : 'border-gray-200 dark:border-gray-700'
                      }`}
                    >
                      <div className="flex items-center gap-2 px-3 py-2">
                        <button
                          onClick={() => toggle(item.id)}
                          className={`w-5 h-5 rounded border-2 flex items-center justify-center shrink-0 transition-colors ${
                            isSelected
                              ? 'border-blue-500 bg-blue-500 text-white'
                              : 'border-gray-300 dark:border-gray-600'
                          }`}
                        >
                          {isSelected && <Check size={12} />}
                        </button>
                        <span className="text-sm text-gray-700 dark:text-gray-300 flex-1 truncate">
                          {item.label}
                        </span>
                        <button
                          onClick={() => setExpandedId(isExpanded ? null : item.id)}
                          className="p-1 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
                        >
                          {isExpanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
                        </button>
                      </div>
                      {isExpanded && (
                        <div className="px-3 pb-2 ml-7">
                          <div className="text-xs text-gray-500 dark:text-gray-400 bg-gray-50 dark:bg-gray-800 rounded p-2 max-h-32 overflow-y-auto">
                            <MarkdownContent content={item.content} className="text-xs" />
                          </div>
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          ))}
        </div>

        {/* Footer */}
        <div className="px-6 py-4 border-t border-gray-200 dark:border-gray-700 flex items-center justify-between">
          <span className="text-xs text-gray-400">
            {items.length === 0 ? 'No context items available — will create with title only' : selected.size === 0 ? 'No context selected — will create with title only' : ''}
          </span>
          <div className="flex gap-2">
            <button onClick={onCancel} className="btn btn-secondary">Cancel</button>
            <button
              onClick={() => { if (entityTitle.trim()) onConfirm(selectedItems, entityTitle.trim()); }}
              disabled={!entityTitle.trim()}
              className={`btn ${entityTitle.trim() ? 'btn-primary' : 'btn-secondary opacity-50 cursor-not-allowed'}`}
            >
              Create {targetLabel}{selected.size > 0 ? ` (${selected.size} items)` : ''}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

/**
 * Helper to build selectable items from an ideation
 */
export function buildIdeationItems(ideation: {
  problem_statement?: string | null;
  proposed_approach?: string | null;
  scope_assessment?: { domains: number; ambiguity: number; dependencies: number } | null;
  complexity?: string | null;
  description?: string | null;
  qa_items?: { question: string; answer: string | null; asked_by: string }[];
}): SelectableItem[] {
  const items: SelectableItem[] = [];

  if (ideation.problem_statement) {
    items.push({
      id: 'problem_statement',
      label: 'Problem Statement',
      content: ideation.problem_statement,
      category: 'Context',
    });
  }
  if (ideation.proposed_approach) {
    items.push({
      id: 'proposed_approach',
      label: 'Proposed Approach',
      content: ideation.proposed_approach,
      category: 'Context',
    });
  }
  if (ideation.description) {
    items.push({
      id: 'description',
      label: 'Description',
      content: ideation.description,
      category: 'Context',
    });
  }
  if (ideation.scope_assessment) {
    const sa = ideation.scope_assessment;
    items.push({
      id: 'scope_assessment',
      label: `Scope Assessment (D:${sa.domains} A:${sa.ambiguity} Dep:${sa.dependencies})`,
      content: `- Domains: ${sa.domains}/5\n- Ambiguity: ${sa.ambiguity}/5\n- Dependencies: ${sa.dependencies}/5\n- Complexity: ${ideation.complexity || 'not evaluated'}`,
      category: 'Context',
    });
  }

  const answered = (ideation.qa_items || []).filter((q) => q.answer);
  answered.forEach((qa, i) => {
    items.push({
      id: `qa_${i}`,
      label: qa.question.length > 80 ? qa.question.slice(0, 77) + '...' : qa.question,
      content: `**Q:** ${qa.question}\n**A:** ${qa.answer}`,
      category: 'Q&A Decisions',
    });
  });

  return items;
}

/**
 * Helper to build selectable items from a refinement
 */
export function buildRefinementItems(refinement: {
  description?: string | null;
  in_scope?: string[] | null;
  out_of_scope?: string[] | null;
  analysis?: string | null;
  decisions?: string[] | null;
  qa_items?: { question: string; answer: string | null; asked_by: string }[];
}): SelectableItem[] {
  const items: SelectableItem[] = [];

  if (refinement.description) {
    items.push({ id: 'description', label: 'Description', content: refinement.description, category: 'Context' });
  }
  (refinement.in_scope || []).forEach((s, i) => {
    items.push({ id: `in_scope_${i}`, label: s.length > 80 ? s.slice(0, 77) + '...' : s, content: s, category: 'In Scope' });
  });
  (refinement.out_of_scope || []).forEach((s, i) => {
    items.push({ id: `out_scope_${i}`, label: s.length > 80 ? s.slice(0, 77) + '...' : s, content: s, category: 'Out of Scope' });
  });
  if (refinement.analysis) {
    items.push({ id: 'analysis', label: 'Analysis', content: refinement.analysis, category: 'Context' });
  }
  (refinement.decisions || []).forEach((d, i) => {
    items.push({ id: `decision_${i}`, label: d.length > 80 ? d.slice(0, 77) + '...' : d, content: d, category: 'Decisions' });
  });

  const answered = (refinement.qa_items || []).filter((q) => q.answer);
  answered.forEach((qa, i) => {
    items.push({
      id: `qa_${i}`,
      label: qa.question.length > 80 ? qa.question.slice(0, 77) + '...' : qa.question,
      content: `**Q:** ${qa.question}\n**A:** ${qa.answer}`,
      category: 'Q&A Decisions',
    });
  });

  return items;
}

/**
 * Compile selected items into a markdown context string
 */
export function compileSelectedContext(items: SelectableItem[]): string {
  const grouped = new Map<string, SelectableItem[]>();
  items.forEach((item) => {
    const list = grouped.get(item.category) || [];
    list.push(item);
    grouped.set(item.category, list);
  });

  const parts: string[] = [];
  grouped.forEach((categoryItems, category) => {
    parts.push(`## ${category}`);
    categoryItems.forEach((item) => {
      parts.push(item.content);
    });
  });

  return parts.join('\n\n');
}
