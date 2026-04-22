/**
 * TechnicalRequirementsTab - TR management with task linkage coverage
 */

import { useState, useMemo } from 'react';
import { Plus, Trash2, Settings, CheckCircle, XCircle, Link, Unlink } from 'lucide-react';
import toast from 'react-hot-toast';
import type { Spec, TechnicalRequirement } from '@/types';

interface TechnicalRequirementsTabProps {
  spec: Spec;
  onUpdate: (trs: TechnicalRequirement[]) => void;
  specCards?: { id: string; title: string; status: string }[];
  onLinkTask?: (trId: string, cardId: string) => Promise<void>;
  onUnlinkTask?: (trId: string, cardId: string) => Promise<void>;
  onSpecUpdate?: (patch: Record<string, any>) => void;
}

/** Normalize a TR entry (string or object) to a TechnicalRequirement object. */
function normalizeTR(tr: string | TechnicalRequirement, fallbackIndex: number): TechnicalRequirement {
  if (typeof tr === 'string') {
    return { id: `tr_legacy_${fallbackIndex}`, text: tr, linked_task_ids: null };
  }
  return tr;
}

export function TechnicalRequirementsTab({ spec, onUpdate, specCards, onLinkTask, onUnlinkTask, onSpecUpdate }: TechnicalRequirementsTabProps) {
  const [draft, setDraft] = useState('');
  const [adding, setAdding] = useState(false);
  const [linkingTrId, setLinkingTrId] = useState<string | null>(null);

  const rawTRs = spec.technical_requirements || [];
  const trs: TechnicalRequirement[] = rawTRs.map((tr, i) => normalizeTR(tr as any, i));

  // Coverage: TRs with at least one linked task
  const coverage = useMemo(() => {
    const withTasks = trs.filter(tr => tr.linked_task_ids && tr.linked_task_ids.length > 0);
    return {
      covered: withTasks.length,
      total: trs.length,
      pct: trs.length > 0 ? Math.round((withTasks.length / trs.length) * 100) : 0,
    };
  }, [trs]);

  const handleAdd = () => {
    const text = draft.trim();
    if (!text) return;
    const id = `tr_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`;
    const newTR: TechnicalRequirement = { id, text, linked_task_ids: null };
    onUpdate([...trs, newTR]);
    setDraft('');
  };

  const handleRemove = (id: string) => {
    onUpdate(trs.filter(tr => tr.id !== id));
  };

  return (
    <div className="space-y-4">
      {/* TR Task Linkage Coverage */}
      {trs.length > 0 && (
        <div className="border border-gray-200 dark:border-gray-700 rounded-lg p-3">
          <div className="flex items-center justify-between mb-2">
            <h4 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide">
              TR Task Coverage ({coverage.covered}/{coverage.total})
            </h4>
            {coverage.covered === coverage.total && coverage.total > 0 ? (
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300 font-medium">
                100% linked
              </span>
            ) : (
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300 font-medium">
                {coverage.pct}% linked
              </span>
            )}
          </div>
          {/* Progress bar */}
          <div className="h-2 bg-gray-100 dark:bg-gray-700 rounded-full overflow-hidden mb-2">
            <div
              className={`h-full transition-all duration-500 rounded-full ${coverage.covered === coverage.total && coverage.total > 0 ? 'bg-green-500' : 'bg-amber-500'}`}
              style={{ width: `${coverage.pct}%` }}
            />
          </div>
          {/* TR list with coverage */}
          <div className="space-y-1 max-h-48 overflow-y-auto">
            {trs.map((tr) => {
              const taskCount = tr.linked_task_ids?.length ?? 0;
              const linked = taskCount > 0;
              return (
                <div key={tr.id} className="flex items-start gap-2 text-xs">
                  {linked ? (
                    <CheckCircle className="w-3.5 h-3.5 text-green-500 shrink-0 mt-0.5" />
                  ) : (
                    <XCircle className="w-3.5 h-3.5 text-gray-300 dark:text-gray-600 shrink-0 mt-0.5" />
                  )}
                  <span className={`flex-1 line-clamp-1 ${linked ? 'text-gray-700 dark:text-gray-300' : 'text-gray-400 dark:text-gray-500'}`}>
                    {tr.text}
                  </span>
                  {linked && (
                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300 shrink-0">
                      {taskCount} task{taskCount !== 1 ? 's' : ''}
                    </span>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Skip TRs coverage toggle */}
      {onSpecUpdate && (
        <div className="flex items-center justify-between px-3 py-2 rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50/50 dark:bg-gray-700/20">
          <div>
            <span className="text-xs font-medium text-gray-700 dark:text-gray-300">Skip TRs coverage requirement</span>
            <p className="text-[10px] text-gray-400">Allow starting cards without full TR→Task coverage</p>
          </div>
          <button
            onClick={() => onSpecUpdate({ skip_trs_coverage: !(spec as any).skip_trs_coverage })}
            className={`relative w-10 h-5 rounded-full transition-colors ${(spec as any).skip_trs_coverage ? 'bg-amber-500' : 'bg-gray-300 dark:bg-gray-600'}`}
          >
            <span className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white transition-transform ${(spec as any).skip_trs_coverage ? 'translate-x-5' : ''}`} />
          </button>
        </div>
      )}

      {/* TR list */}
      {trs.length === 0 && !adding && (
        <div className="text-center py-6">
          <Settings size={32} className="mx-auto text-gray-300 dark:text-gray-600 mb-2" />
          <p className="text-sm text-gray-500 dark:text-gray-400">No technical requirements</p>
          <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">Technical constraints and architectural decisions</p>
        </div>
      )}

      {trs.map((tr) => {
        const taskCount = tr.linked_task_ids?.length ?? 0;
        const linkedTaskIds = new Set(tr.linked_task_ids || []);
        const availableCards = (specCards || []).filter(c => !linkedTaskIds.has(c.id));
        return (
          <div key={tr.id} className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
            <div className="flex items-start gap-2 px-3 py-2 group">
              <Settings size={14} className="text-gray-400 shrink-0 mt-0.5" />
              <span className="text-sm text-gray-700 dark:text-gray-300 flex-1">{tr.text}</span>
              {taskCount > 0 ? (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300 shrink-0">
                  {taskCount} task{taskCount !== 1 ? 's' : ''}
                </span>
              ) : (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-100 text-gray-400 dark:bg-gray-700 dark:text-gray-500 shrink-0">
                  0 tasks
                </span>
              )}
              {onLinkTask && availableCards.length > 0 && (
                <button onClick={() => setLinkingTrId(linkingTrId === tr.id ? null : tr.id)} className="p-0.5 text-blue-400 hover:text-blue-600 shrink-0" title="Link task">
                  <Link size={12} />
                </button>
              )}
              <button onClick={() => handleRemove(tr.id)} className="opacity-0 group-hover:opacity-100 p-0.5 text-red-400 hover:text-red-600 transition-opacity shrink-0">
                <Trash2 size={12} />
              </button>
            </div>
            {/* Linked tasks */}
            {taskCount > 0 && (
              <div className="px-3 pb-2 space-y-1">
                {(tr.linked_task_ids || []).map(taskId => {
                  const card = (specCards || []).find(c => c.id === taskId);
                  return (
                    <div key={taskId} className="flex items-center justify-between text-xs px-2 py-1 rounded bg-green-50 dark:bg-green-900/10 group/task">
                      <span className="text-gray-600 dark:text-gray-400 truncate">{card?.title || taskId.slice(0, 8) + '…'}</span>
                      {onUnlinkTask && (
                        <button onClick={async () => { try { await onUnlinkTask(tr.id, taskId); toast.success('Task unlinked'); } catch { toast.error('Failed'); } }} className="p-0.5 text-gray-400 hover:text-red-500 opacity-0 group-hover/task:opacity-100 shrink-0">
                          <Unlink size={10} />
                        </button>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
            {/* Card picker */}
            {linkingTrId === tr.id && availableCards.length > 0 && (
              <div className="px-3 pb-2 border-t border-gray-100 dark:border-gray-700 pt-2 space-y-1 max-h-32 overflow-y-auto">
                <p className="text-[10px] text-gray-400">Click to link:</p>
                {availableCards.map(card => (
                  <button key={card.id} onClick={async () => { try { await onLinkTask!(tr.id, card.id); setLinkingTrId(null); toast.success('Task linked'); } catch { toast.error('Failed'); } }} className="w-full flex items-center gap-2 px-2 py-1 rounded text-xs text-left hover:bg-blue-50 dark:hover:bg-blue-900/20">
                    <span className="text-gray-600 dark:text-gray-400 truncate">{card.title}</span>
                    <Link size={10} className="text-gray-400 shrink-0 ml-auto" />
                  </button>
                ))}
              </div>
            )}
          </div>
        );
      })}

      {/* Add form */}
      {adding ? (
        <div className="flex gap-2">
          <input
            type="text"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleAdd()}
            placeholder="Add a technical constraint..."
            className="flex-1 px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600"
            autoFocus
          />
          <button onClick={handleAdd} disabled={!draft.trim()} className="btn btn-primary text-xs">Add</button>
          <button onClick={() => { setAdding(false); setDraft(''); }} className="btn btn-secondary text-xs">Cancel</button>
        </div>
      ) : (
        <button onClick={() => setAdding(true)} className="flex items-center gap-1 text-sm text-blue-600 dark:text-blue-400 hover:text-blue-800 dark:hover:text-blue-300">
          <Plus size={14} /> Add Technical Requirement
        </button>
      )}
    </div>
  );
}
